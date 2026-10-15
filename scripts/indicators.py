#!/usr/bin/env python3
"""每日指標檔：讀 data/prices_adj.csv（還原價）與 data/positions.json，輸出 data/indicators.json。

每檔（WATCHLIST + GLOBAL 的股票/ETF；匯率只列最新價）輸出：
  data_date            該檔最後一根完整 K 棒的日期（meta.json 的 partial_bars 會先剔除）
  ma5/10/20/60         收盤價簡單移動平均（還原價）；根數不足為 null
  atr14, atr14_pct     Wilder ATR(14)（第一個值為前 14 根 TR 的平均，之後 (前值*13+TR)/14），占收盤價 %
  stop_*               有進場價才算：停損 = 進場價 - atr_mult×ATR，停損幅度最多 max_stop_pct%（取較高者）
  ret5_pct, rs5_vs_0050_pct   近 5 根 K 棒報酬；相對 0050 強弱 = 個股報酬 - 0050 同期間報酬（百分點）
                       0050 的起訖價取「個股起訖日當天或之前最近一個 0050 收盤」，日期寫在 rs5_ref_dates；
                       兩邊日期沒對齊（美股、韓股）時 rs5_aligned=false，數字僅供參考。
  status               ok / stale（資料日期落後同市場最新日期）/ insufficient_data
所有數字由本腳本計算，不含估算。

用法：python scripts/indicators.py
"""
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from fetch_prices import GLOBAL, WATCHLIST, global_kind  # noqa: E402

PRICES_ADJ = os.path.join(ROOT, "data", "prices_adj.csv")
META = os.path.join(ROOT, "data", "meta.json")
POSITIONS = os.path.join(ROOT, "data", "positions.json")
OUT = os.path.join(ROOT, "data", "indicators.json")
BENCH = "0050"
TZ_TW = timezone(timedelta(hours=8))


def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def wilder_atr(bars, n=14):
    """bars: [(high, low, close)]，由舊到新。回傳最新一根的 ATR，根數不足（需要 n+1 根）回傳 None。"""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, _ = bars[i]
        pc = bars[i - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def r4(x):
    return None if x is None else round(x, 4)


def r2(x):
    return None if x is None else round(x, 2)


def asof(series_dates, series_close, date):
    """date 當天或之前最近一根的 (日期, 收盤)；沒有回傳 None。series_dates 已排序。"""
    lo, hi, best = 0, len(series_dates) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series_dates[mid] <= date:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return None if best is None else (series_dates[best], series_close[best])


def main():
    meta = json.load(open(META, encoding="utf-8")) if os.path.exists(META) else {}
    partial = meta.get("partial_bars", {})
    pos = json.load(open(POSITIONS, encoding="utf-8")) if os.path.exists(POSITIONS) else {}
    entries = pos.get("entries", {})
    atr_mult = float(pos.get("atr_mult", 2))
    max_stop = float(pos.get("max_stop_pct", 10))

    bars = {}
    with open(PRICES_ADJ, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["date"] in partial.get(r["code"], []):
                continue
            try:
                bars.setdefault(r["code"], []).append(
                    (r["date"], float(r["high"]), float(r["low"]), float(r["close"])))
            except ValueError:
                continue
    for c in bars:
        bars[c].sort()

    def market(code):
        if code in WATCHLIST:
            return "TW"
        return {"KRX": "KR", "FX": "FX", "NYSE": "US"}[global_kind(code)]

    latest_by_market = {}
    for c, b in bars.items():
        m = market(c)
        latest_by_market[m] = max(latest_by_market.get(m, ""), b[-1][0])

    bench = bars.get(BENCH, [])
    b_dates = [x[0] for x in bench]
    b_close = [x[3] for x in bench]

    names = {c: v[0] for c, v in WATCHLIST.items()}
    names.update(GLOBAL)
    items = {}
    for code in list(WATCHLIST) + list(GLOBAL):
        b = bars.get(code)
        if not b:
            items[code] = {"name": names.get(code), "market": market(code), "status": "no_data"}
            continue
        m = market(code)
        dates = [x[0] for x in b]
        closes = [x[3] for x in b]
        close, data_date = closes[-1], dates[-1]
        it = {"name": names.get(code), "market": m, "data_date": data_date, "close": r4(close), "bars": len(b),
              "status": "ok" if data_date == latest_by_market[m] else "stale"}
        if data_date in partial.get(code, []):  # 理論上已剔除，保險
            it["status"] = "partial_bar"
        if m == "FX":
            items[code] = it
            continue
        it.update({f"ma{n}": r4(sma(closes, n)) for n in (5, 10, 20, 60)})
        atr = wilder_atr([(x[1], x[2], x[3]) for x in b], 14)
        it["atr14"] = r4(atr)
        it["atr14_pct"] = r2(atr / close * 100) if atr else None
        if atr is None or it["ma60"] is None:
            it["status"] = "insufficient_data" if it["status"] == "ok" else it["status"]

        entry = entries.get(code)
        it["entry_price"] = entry
        if entry and atr:
            raw_stop = entry - atr_mult * atr
            floor = entry * (1 - max_stop / 100)
            stop = max(raw_stop, floor)
            it.update({"stop_price": r4(stop), "stop_pct": r2((stop / entry - 1) * 100),
                       "stop_basis": "capped_at_max_stop_pct" if raw_stop < floor else f"entry-{atr_mult:g}xATR14",
                       "close_vs_stop_pct": r2((close / stop - 1) * 100)})
        else:
            it.update({"stop_price": None, "stop_pct": None, "stop_basis": None, "close_vs_stop_pct": None})

        if len(closes) > 5:
            d_start, c_start = dates[-6], closes[-6]
            it["ret5_pct"] = r2((close / c_start - 1) * 100)
            it["ret5_dates"] = [d_start, data_date]
            if code == BENCH:
                it.update({"rs5_vs_0050_pct": 0.0, "rs5_ref_dates": [d_start, data_date], "rs5_aligned": True})
            else:
                s = asof(b_dates, b_close, d_start)
                e = asof(b_dates, b_close, data_date)
                if s and e:
                    ret_b = e[1] / s[1] - 1
                    it["rs5_vs_0050_pct"] = r2((close / c_start - 1 - ret_b) * 100)
                    it["rs5_ref_dates"] = [s[0], e[0]]
                    it["rs5_aligned"] = (s[0] == d_start and e[0] == data_date)
                    it["bench_ret5_pct"] = r2(ret_b * 100)
        items[code] = it

    out = {"generated_at": datetime.now(TZ_TW).isoformat(timespec="seconds"),
           "price_basis": "prices_adj.csv（台股已還原分割/除權息；GLOBAL 為 yfinance 原始價）",
           "as_of_by_market": latest_by_market,
           "partial_bars_excluded": partial,
           "stale": sorted(c for c, v in items.items() if v.get("status") not in ("ok",)),
           "params": {"atr_period": 14, "atr_mult": atr_mult, "max_stop_pct": max_stop, "bench": BENCH},
           "indicators": items}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"寫入 {OUT}（{len(items)} 檔；非 ok：{out['stale']}）", file=sys.stderr)


if __name__ == "__main__":
    main()
