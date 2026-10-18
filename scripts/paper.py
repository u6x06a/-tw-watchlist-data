#!/usr/bin/env python3
"""階段 4：規則模擬帳戶。完全照規則買賣（scripts/sim.py），從 config paper.start_date 起模擬到最新交易日，輸出 data/paper.json。

內容：目前持倉（股數、均價、停損、所屬規則）、全部成交明細（含規則代號與出場原因）、每日淨值、0050 基準淨值
（同一天以同樣資金買進 0050 持有，還原含息）、各規則統計（筆數、勝率、平均 R、平均持有天數、最大虧損）、總報酬、最大回撤。
L2 與 needs_news_check 的訊號預設不執行，只記錄（not_executed_counts）；config paper.execute 可打開比較。
此帳戶為規則模擬，與實際持股無關；價格為還原價，成交以隔日開盤價計。

用法：python scripts/paper.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, log, now_tw, path, write_json  # noqa: E402
from panel import Panel  # noqa: E402
from screen import clean  # noqa: E402
from sim import Sim  # noqa: E402


def main():
    cfg = load_config()
    P = Panel(cfg)
    sim = Sim(P, cfg).run()
    rep = sim.report(int(cfg["backtest"]["sample_min"]))
    out = {"generated_at": now_tw().isoformat(timespec="seconds"), "data_date": P.dates[-1],
           "notes": ["規則模擬帳戶，不做主觀判斷；與實際持股無關",
                     "訊號在收盤產生、隔日開盤成交；價格為還原價（含息），實際成交價會不同",
                     "樣本少於 config backtest.sample_min 筆的統計標「樣本不足，僅供參考」"],
           "params": cfg["paper"], **rep}
    write_json(path("data", "paper.json"), clean(out), compact=True)
    log(f"paper.json：{rep['start_date']}~{rep['end_date']} 總報酬 {rep['total_return_pct']}% "
        f"（0050 {rep['bench_return_pct']}%）MDD {rep['max_drawdown_pct']}%；成交 {len(rep['trades'])} 筆；"
        f"持倉 {len(rep['positions'])}；統計 {rep['stats_by_rule']}")


if __name__ == "__main__":
    main()
