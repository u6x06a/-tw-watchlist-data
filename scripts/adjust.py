#!/usr/bin/env python3
"""由 data/prices.csv（原始、未還原）產生 data/prices_adj.csv（還原版）與 data/adjustments.json。

原始檔不動。prices_adj.csv 欄位與 prices.csv 相同（code,date,open,high,low,close,volume），
只收 WATCHLIST 與 GLOBAL 代號，下游程式可直接替換檔名。

還原事件一律來自交易所公布數字，不是估算：
  - 除權息：證交所 TWT49U、櫃買 exDailyQ 的「除權息前收盤價 / 除權息參考價」，
    還原係數 = 參考價 / 前收盤價（現金息與配股都適用）；每次執行重新抓取，網路失敗時退回 data/events_auto.json 快取。
  - 分割（交易所除權息表不含）：手動登錄在 SPLITS。
      00685L 2026-07-07：前收 306.00，證交所當日參考價 12.75（STOCK_DAY 漲跌 -0.52 反推）-> 24:1
價格往前乘係數；成交量只在分割時反向調整（除權息不調整成交量）。
美股/韓股/匯率（GLOBAL）由 yfinance 提供，不在此還原（yfinance 的分割本來就已調整，股利未調整）。

用法：python scripts/adjust.py [--check] [--no-fetch]
  --check     列出還原後仍有 |單日漲跌| > 10.5% 的台股代號日，以及除權息前收盤與 CSV 前一日收盤不符的事件
  --no-fetch  不連網，只用快取的除權息事件
"""
import csv
import json
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from fetch_prices import GLOBAL, WATCHLIST  # noqa: E402

SRC = os.path.join(ROOT, "data", "prices.csv")
DST = os.path.join(ROOT, "data", "prices_adj.csv")
LOG = os.path.join(ROOT, "data", "adjustments.json")
CACHE = os.path.join(ROOT, "data", "events_auto.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; watchlist-bot/1.0)"}

# ratio 為 1 拆 ratio，生效日（含）起為新價；生效日以前的價格 / ratio、成交量 * ratio
SPLITS = {
    "00685L": [
        {"effective": "2026-07-07", "ratio": 24,
         "source": "TWSE STOCK_DAY 2026-07-07：前收 306.00，參考價 12.75（收盤 12.23、漲跌 -0.52）"},
    ],
}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def _roc(s):
    """'115年07月21日' 或 '115/07/21' -> '2026-07-21'"""
    y, m, d = map(int, re.findall(r"\d+", s)[:3])
    return f"{y + 1911:04d}-{m:02d}-{d:02d}"


def _get_json(url, params):
    last = None
    for i in range(3):
        try:
            with urlopen(Request(url + "?" + urlencode(params), headers=HEADERS), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise last


def fetch_events(start, end, tracked):
    """回傳 [{code,date,prev_close,ref,factor,market}]，只含 tracked 代號。start/end 為 ISO 日期。"""
    out = []
    s, e = start.replace("-", ""), end.replace("-", "")
    j = _get_json("https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
                  {"startDate": s, "endDate": e, "response": "json"})
    for d in j.get("data", []) if j.get("stat") == "OK" else []:
        if d[1].strip() in tracked:
            out.append(("TWSE", d))
    time.sleep(3)
    j = _get_json("https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
                  {"startDate": start.replace("-", "/"), "endDate": end.replace("-", "/"), "response": "json"})
    for t in j.get("tables", []):
        for d in t.get("data", []):
            if d[1].strip() in tracked:
                out.append(("TPEX", d))
    events = []
    for market, d in out:
        prev, ref = _num(d[3]), _num(d[4])
        if not prev or not ref:
            continue
        events.append({"code": d[1].strip(), "date": _roc(d[0]), "prev_close": prev, "ref": ref,
                       "factor": ref / prev, "kind": d[6 if market == "TWSE" else 8].strip(), "market": market})
    return sorted(events, key=lambda x: (x["code"], x["date"]))


def load_events(first_date, last_date, tracked, no_fetch):
    status = "cache"
    events = None
    if not no_fetch:
        try:
            events = fetch_events(first_date, last_date, tracked)
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=1)
            status = "fetched"
        except Exception as e:  # noqa: BLE001
            print(f"除權息事件抓取失敗，改用快取：{type(e).__name__}: {e}", file=sys.stderr)
            status = f"fetch failed ({type(e).__name__}), cache"
    if events is None:
        events = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else []
        if not events:
            status += "（沒有快取，除權息未還原）"
    return events, status


def build_factors(events):
    """{code: [(effective_date, price_factor, volume_factor), ...]}；date 早於 effective 才套用。"""
    f = {}
    for ev in events:
        f.setdefault(ev["code"], []).append((ev["date"], ev["factor"], 1.0))
    for code, lst in SPLITS.items():
        for sp in lst:
            f.setdefault(code, []).append((sp["effective"], 1.0 / sp["ratio"], float(sp["ratio"])))
    return f


def factors(table, code, date):
    pf, vf = 1.0, 1.0
    for eff, p, v in table.get(code, []):
        if date < eff:
            pf *= p
            vf *= v
    return pf, vf


def main():
    tracked = set(WATCHLIST) | set(GLOBAL)
    rows = []
    with open(SRC, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["code"] in tracked:
                rows.append(r)
    dates = sorted({r["date"] for r in rows})
    tw_codes = set(WATCHLIST)
    events, ev_status = load_events(dates[0], dates[-1], tw_codes, "--no-fetch" in sys.argv)
    table = build_factors(events)

    raw_close = {}
    out_rows, adj_close = [], {}
    for r in rows:
        c = r["code"]
        raw_close[(c, r["date"])] = float(r["close"])
        pf, vf = factors(table, c, r["date"])
        if pf != 1.0 or vf != 1.0:
            for k in ("open", "high", "low", "close"):
                if r[k] != "":
                    r[k] = f"{float(r[k]) * pf:.4f}".rstrip("0").rstrip(".")
            if r["volume"] != "" and vf != 1.0:
                r["volume"] = f"{float(r['volume']) * vf:.1f}"
        out_rows.append(r)
        adj_close.setdefault(c, []).append((r["date"], float(r["close"])))
    out_rows.sort(key=lambda r: (r["code"], r["date"]))
    with open(DST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(out_rows)

    # 事件前收盤與 CSV 前一個交易日收盤是否一致（不一致代表資料缺口或事件來源有誤）
    by_code_dates = {}
    for (c, d) in raw_close:
        by_code_dates.setdefault(c, []).append(d)
    mismatches = []
    for ev in events:
        ds = sorted(by_code_dates.get(ev["code"], []))
        prevs = [d for d in ds if d < ev["date"]]
        if not prevs:
            continue  # 事件早於我們的資料起點，不影響
        csv_prev = raw_close[(ev["code"], prevs[-1])]
        if abs(csv_prev - ev["prev_close"]) > 0.011:
            mismatches.append({"code": ev["code"], "date": ev["date"], "event_prev_close": ev["prev_close"],
                               "csv_prev_close": csv_prev, "csv_prev_date": prevs[-1]})
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump({"events_status": ev_status,
                   "events": [e for e in events if e["date"] >= dates[0]],
                   "splits": SPLITS,
                   "event_prev_close_mismatch": mismatches,
                   "not_adjusted": "GLOBAL 代號不還原；成交量只在分割時調整。",
                   "rows": len(out_rows)}, f, ensure_ascii=False, indent=2)
    print(f"寫入 {DST}（{len(out_rows)} 筆；除權息事件 {len(events)} 筆，來源：{ev_status}；前收不符 {len(mismatches)} 筆）",
          file=sys.stderr)

    if "--check" in sys.argv:
        for m in mismatches:
            print(f"前收不符：{m}")
        bad = 0
        for c, series in sorted(adj_close.items()):
            if c in GLOBAL:
                continue
            series.sort()
            for (d0, c0), (d1, c1) in zip(series, series[1:]):
                ret = c1 / c0 - 1
                if abs(ret) > 0.105:
                    bad += 1
                    print(f"疑似未登錄事件/壞資料：{c} {d1} {c0:.4f}->{c1:.4f} ({ret * 100:+.2f}%)")
        print(f"檢查完成，{bad} 筆超過 10.5%", file=sys.stderr)


if __name__ == "__main__":
    main()
