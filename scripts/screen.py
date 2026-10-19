#!/usr/bin/env python3
"""階段 3：每日選股。用 scripts/rules.evaluate() 評估最新交易日，輸出 data/signals.json 與 data/flags.json。

signals.json：當日所有觸發的訊號（R1 右側、L1/L2 左側、E1 ETF 階梯、C1 籌碼轉強），每筆含代號、名稱、產業、規則代號、
  關鍵數字、參考進場價、建議停損、每股風險、產業排名、RS 排名、data_date、needs_news_check、market_filtered 等。
  排序：規則分組，組內依趨勢模板、RS 排名、產業排名由強到弱。只輸出客觀計算，不含持股與買賣建議。
flags.json：全市場流動股的賣出與警示旗標（儀表板端自行比對持股），與 market_wide_selloff。
資料日期不是最新交易日時（meta.json markets 不一致），在檔案中標 data_stale=true。

用法：python scripts/screen.py [--date YYYY-MM-DD]
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, log, now_tw, path, read_json, write_json  # noqa: E402
from panel import Panel  # noqa: E402
from rules import evaluate  # noqa: E402


def clean(x):
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if np.isnan(x) else float(x)
    return x


def main():
    cfg = load_config()
    P = Panel(cfg)
    i = len(P.dates) - 1
    if "--date" in sys.argv:
        i = P.idx(sys.argv[sys.argv.index("--date") + 1])
    r = evaluate(P, i)
    meta = read_json(path("data", "meta.json"), {}) or {}
    tw = (meta.get("markets") or {}).get("TWSE") or (meta.get("markets") or {}).get("TW") or {}
    stale = tw.get("expected_latest") not in (None, r["date"])
    common = {"generated_at": now_tw().isoformat(timespec="seconds"), "data_date": r["date"],
              "expected_latest": tw.get("expected_latest"), "data_stale": bool(stale),
              "market_filtered": bool(r["market"].get("filtered")), "market_wide_selloff": r["market_wide_selloff"],
              "quarter_end_window": r["quarter_end_window"], "chips_data_available": r["chips_data"]}
    by_rule = {}
    for s in r["signals"]:
        by_rule[s["rule"]] = by_rule.get(s["rule"], 0) + 1
    write_json(path("data", "signals.json"), clean({
        **common, "counts": by_rule, "c1_note": r["c1_note"], "skipped_adj_uncertain": r["skipped"],
        "notes": ["訊號只代表規則條件成立，不是買賣建議；needs_news_check=true 的項目需讀新聞確認沒有個股專屬利空",
                  "market_filtered=true：大盤過濾中（收盤在季線下或跌破恐慌日低點），訊號照常列出供參考",
                  "外資買賣超可能含 ETF 被動調整與避險，單日數字意義有限"],
        "params": cfg["screen"], "signals": r["signals"]}))
    write_json(path("data", "flags.json"), clean({
        **common, "notes": ["market_wide_selloff=true（0050 當日跌 ≥ 2%）：儀表板遇到這天觸發停損，改為先減半、隔日收盤確認",
                            "window_dressing_possible：季底月份最後 10 個交易日，投信買超可能為作帳"],
        "counts": {"stocks_flagged": len(r["flags"])}, "flags": r["flags"]}))
    log(f"signals.json：{r['date']} {by_rule}；flags.json：{len(r['flags'])} 檔；大盤過濾中={common['market_filtered']}")


if __name__ == "__main__":
    main()
