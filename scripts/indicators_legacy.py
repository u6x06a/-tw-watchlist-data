#!/usr/bin/env python3
"""舊版指標（相容既有讀取者）：讀 data/prices_adj.csv（關注名單與海外代號的還原價），由 scripts/indicators.py 呼叫 build()，
結果放在 data/indicators.json 的 indicators 區塊（格式與舊版相同）。

每檔（WATCHLIST + GLOBAL 的股票/ETF；匯率只列最新價）輸出：
  data_date            該檔最後一根完整 K 棒的日期（meta.json 的 partial_bars 會先剔除）
  ma5/10/20/60         收盤價簡單移動平均（還原價）；根數不足為 null
  atr14, atr14_pct     Wilder ATR(14)（第一個值為前 14 根 TR 的平均，之後 (前值*13+TR)/14），占收盤價 %
  停損               不放進公開的 indicators.json（repo 是公開的，持股是私人資料）。本機有 positions.local.json 時，
                       另外輸出 stops.local.json：每一批各算一個停損 = 進場價(還原基準) - atr_mult×ATR，幅度最多 max_stop_pct%
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
# 持股明細是私人資料，repo 是公開的：只讀本機的 positions.local.json（已列入 .gitignore），停損結果寫到 stops.local.json
POSITIONS_LOCAL = os.path.join(ROOT, "positions.local.json")
STOPS_LOCAL = os.path.join(ROOT, "stops.local.json")
EVENTS = os.path.join(ROOT, "data", "events_auto.json")
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


def build():
    """舊版指標（關注名單 + 海外代號），回傳 dict，不寫檔；由 scripts/indicators.py 併入 indicators.json 的 indicators 區塊。"""
    meta = json.load(open(META, encoding="utf-8")) if os.path.exists(META) else {}
    partial = meta.get("partial_bars", {})
    pos = json.load(open(POSITIONS_LOCAL, encoding="utf-8")) if os.path.exists(POSITIONS_LOCAL) else {}
    atr_mult = float(pos.get("atr_mult", 2))
    max_stop = float(pos.get("max_stop_pct", 10))

    bars = {}
    tracked = set(WATCHLIST) | set(GLOBAL)   # prices_adj.csv 已含全市場；本檔只處理關注名單與海外代號
    with open(PRICES_ADJ, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["code"] not in tracked or r["date"] in partial.get(r["code"], []):
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

    # ---- 持股停損（每批分開算）：只在本機有 positions.local.json 時輸出到 stops.local.json，不進公開的 data/ ----
    if pos.get("entries"):
        write_stops(pos, items, atr_mult, max_stop)
    return out


def write_stops(pos, items, atr_mult, max_stop):
    """每一批（lot）各算一個停損：停損 = 進場價(還原基準) - atr_mult×ATR14，幅度最多 max_stop%。
    進場價先換算到今天的還原價基準（乘上該批成交日之後的除權息係數；分割同理，標 split_adjusted 的批次除外），
    這樣才和 ATR、收盤價在同一個價格尺度。"""
    from adjust import lot_price_factor
    events = json.load(open(EVENTS, encoding="utf-8")) if os.path.exists(EVENTS) else []
    out, missing = {}, []
    for code, lots in pos["entries"].items():
        it = items.get(code)
        if not it or not it.get("atr14"):
            missing.append(code)
            continue
        atr, close = it["atr14"], it["close"]
        rows = []
        for lot in lots:
            f = lot_price_factor(code, lot["date"], events, lot.get("price_basis") == "split_adjusted")
            e = lot["price"] * f
            raw_stop = e - atr_mult * atr
            floor = e * (1 - max_stop / 100)
            stop = max(raw_stop, floor)
            rows.append({"date": lot["date"], "shares": lot["shares"], "entry_price": lot["price"],
                         "entry_price_adj": r4(e), "stop_price": r4(stop), "stop_pct": r2((stop / e - 1) * 100),
                         "stop_basis": "capped_at_max_stop_pct" if raw_stop < floor else f"entry-{atr_mult:g}xATR14",
                         "close_vs_stop_pct": r2((close / stop - 1) * 100), "pnl_vs_entry_pct": r2((close / e - 1) * 100),
                         "below_stop": close <= stop})
        out[code] = {"name": it["name"], "data_date": it["data_date"], "close": close, "atr14": atr,
                     "atr14_pct": it["atr14_pct"], "status": it["status"], "lots": rows,
                     "tightest_close_vs_stop_pct": min(r["close_vs_stop_pct"] for r in rows),
                     "lots_below_stop": sum(r["below_stop"] for r in rows)}
    doc = {"generated_at": datetime.now(TZ_TW).isoformat(timespec="seconds"),
           "params": {"atr_mult": atr_mult, "max_stop_pct": max_stop},
           "no_indicator_data": missing, "positions": out}
    with open(STOPS_LOCAL, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"寫入 {STOPS_LOCAL}（{len(out)} 檔；無指標資料：{missing}）", file=sys.stderr)


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False)[:2000])
