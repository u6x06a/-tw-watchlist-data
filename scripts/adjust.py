#!/usr/bin/env python3
"""由 data/prices.csv（原始、未還原）產生 data/prices_adj.csv（還原版）與 data/adjustments.json。

原始檔不動。prices_adj.csv 欄位與 prices.csv 相同（code,date,open,high,low,close,volume），
只收 WATCHLIST 與 GLOBAL 代號，下游程式可直接替換檔名。

還原事件（EVENTS）一律來自證交所公布數字，不是估算：
  - 00685L 2026-07-07 分割：前收 306.00，證交所當日參考價 12.75（STOCK_DAY 漲跌 -0.52 反推）-> 24:1
  - 0050  2026-07-21 除息：除息前收盤 99.20、參考價 98.60、息值 0.60（證交所 TWT49U）
其他代號的除息尚未還原（見 data/adjustments.json 的 "not_adjusted"）。

用法：python scripts/adjust.py [--check]
  --check  另外列出還原後仍有 |單日漲跌| > 10.5% 的台股代號日（台股漲跌幅限制 10%，超過代表可能有未登錄的分割/壞資料）
"""
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from fetch_prices import GLOBAL, WATCHLIST  # noqa: E402

SRC = os.path.join(ROOT, "data", "prices.csv")
DST = os.path.join(ROOT, "data", "prices_adj.csv")
LOG = os.path.join(ROOT, "data", "adjustments.json")

# type=split：ratio 為 1 拆 ratio，生效日（含）起為新價；type=dividend：prev_close/dividend 為除息前收盤與息值
EVENTS = {
    "00685L": [
        {"type": "split", "effective": "2026-07-07", "ratio": 24,
         "source": "TWSE STOCK_DAY 2026-07-07：前收 306.00，參考價 12.75（收盤 12.23、漲跌 -0.52）"},
    ],
    "0050": [
        {"type": "dividend", "effective": "2026-07-21", "prev_close": 99.20, "dividend": 0.60,
         "source": "TWSE TWT49U 除權息計算結果表：除息前收盤 99.20、參考價 98.60、息值 0.60"},
    ],
}
NOT_ADJUSTED = "其餘代號的除息（如 2330、2308、00735 等）尚未還原；使用者目前只確認 00685L 分割與 0050 除息。"


def factors(code, date):
    """回傳 (價格乘數, 成交量乘數)，date 早於事件生效日才還原。"""
    pf, vf = 1.0, 1.0
    for ev in EVENTS.get(code, []):
        if date < ev["effective"]:
            if ev["type"] == "split":
                pf /= ev["ratio"]
                vf *= ev["ratio"]
            else:
                pf *= (ev["prev_close"] - ev["dividend"]) / ev["prev_close"]
    return pf, vf


def main():
    tracked = set(WATCHLIST) | set(GLOBAL)
    out_rows, adj_close = [], {}
    with open(SRC, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            c = r["code"]
            if c not in tracked:
                continue
            pf, vf = factors(c, r["date"])
            if pf != 1.0 or vf != 1.0:
                for k in ("open", "high", "low", "close"):
                    if r[k] != "":
                        r[k] = f"{float(r[k]) * pf:.4f}".rstrip("0").rstrip(".")
                if r["volume"] != "":
                    r["volume"] = f"{float(r['volume']) * vf:.1f}"
            out_rows.append(r)
            adj_close.setdefault(c, []).append((r["date"], float(r["close"])))
    out_rows.sort(key=lambda r: (r["code"], r["date"]))
    with open(DST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(out_rows)
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump({"events": EVENTS, "not_adjusted": NOT_ADJUSTED, "rows": len(out_rows)}, f, ensure_ascii=False, indent=2)
    print(f"寫入 {DST}（{len(out_rows)} 筆，{len(adj_close)} 檔）", file=sys.stderr)

    if "--check" in sys.argv:
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
