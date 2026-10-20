#!/usr/bin/env python3
"""出場輔助數據（只輸出事實數字，不做買賣判斷）。

給「發財儀表板」的同步排程在雲端本機執行，輸入是「代號 → 各批進場日期」（只有日期，沒有成本與股數），
輸出寫到 --out 指定的 JSON，不寫進 repo、不 commit。

用法：
    python3 scripts/exit_helper.py --lots lots.json --out out.json
lots.json 格式：{"2408": ["2026-10-01", "2026-10-13"], "2330": ["2026-03-09"]}

輸出 out.json：
{
 "asOf": "<最新資料日>",
 "peaks": {"2408": {"dataDate": "2026-10-20", "lots": {"2026-10-01": {"peak": 543.0, "peakDate": "2026-08-31"|..., "close": 422.0}}}},
 "rev":   {"2408": {"latestYm": "2026-09", "yoy": [最近 N 個月 yoy_pct，舊→新], "negStreak": 連續為負的月數}}
}
peak = 進場日（含）至最新資料日的最高「還原收盤價」，與 indicators.json 同一個還原基準（adjlib）。
rev 僅台股有（revenue.csv）；沒有資料的代號不會出現在 rev。
"""
import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adjlib import load_adjusted  # noqa: E402
from common import path  # noqa: E402


def neg_streak(vals):
    n = 0
    for v in reversed(vals):
        if v is not None and v < 0:
            n += 1
        else:
            break
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    lots = json.load(open(a.lots, encoding="utf-8"))
    codes = sorted(lots)
    df = load_adjusted(codes=set(codes))
    out = {"asOf": None, "peaks": {}, "rev": {}}
    latest = []
    for c in codes:
        g = df[df["code"] == c].sort_values("date")
        if not len(g):
            continue
        last_date = g["date"].iloc[-1]
        latest.append(last_date)
        res = {}
        for d in sorted(set(lots[c])):
            sub = g[g["date"] >= d]
            if not len(sub):
                continue
            i = sub["close"].idxmax()
            res[d] = {"peak": round(float(sub.loc[i, "close"]), 4), "peakDate": sub.loc[i, "date"],
                      "close": round(float(sub["close"].iloc[-1]), 4)}
        out["peaks"][c] = {"dataDate": last_date, "lots": res}
    out["asOf"] = max(latest) if latest else None

    rp = path("data", "revenue.csv")
    if os.path.exists(rp):
        rev = pd.read_csv(rp, dtype={"code": str, "ym": str})
        for c in codes:
            g = rev[rev["code"] == c].sort_values("ym")
            if not len(g):
                continue
            yoy = [None if pd.isna(x) else float(x) for x in g["yoy_pct"].tolist()][-6:]
            out["rev"][c] = {"latestYm": g["ym"].iloc[-1], "yoy": yoy, "negStreak": neg_streak(yoy)}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("ok", len(out["peaks"]), "codes, asOf", out["asOf"])


if __name__ == "__main__":
    main()
