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

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, log, now_tw, path, write_json  # noqa: E402
from panel import Panel  # noqa: E402
from screen import clean  # noqa: E402
from sim import Sim  # noqa: E402


def forward_block(rep, cfg, min_n):
    """前向測試：從 paper.forward_start 之後才產生訊號（隔日成交）的交易才算。
    帳戶淨值報酬以 forward_start 前一個交易日收盤為基準（含重播持倉的損益，因為同一個帳戶）。
    規則統計只算前向進場的交易：訊號日 >= forward_start，成交日 > forward_start。"""
    fs = (cfg["paper"] or {}).get("forward_start")
    if not fs:
        return None
    nav = rep["nav"]
    base_i = max((i for i, x in enumerate(nav) if x["date"] < fs), default=None)
    fwd = [x for x in nav if x["date"] >= fs]
    out = {"start_date": fs, "days": len(fwd), "has_data": bool(fwd and base_i is not None)}
    if out["has_data"]:
        b = nav[base_i]
        tot = [b["total"]] + [x["total"] for x in fwd]
        bn = [b["bench_0050"]] + [x["bench_0050"] for x in fwd]

        def mdd(x):
            x = np.array([v for v in x if v], float)
            return round(float(((x / np.maximum.accumulate(x)) - 1).min() * 100), 2) if len(x) else None
        out.update({"base_date": b["date"], "base_total": b["total"], "base_bench": b["bench_0050"],
                    "return_pct": round((tot[-1] / tot[0] - 1) * 100, 2),
                    "bench_return_pct": round((bn[-1] / bn[0] - 1) * 100, 2) if bn[0] else None,
                    "max_drawdown_pct": mdd(tot), "bench_max_drawdown_pct": mdd(bn)})
    closed = [c for c in rep["closed_trades"] if c["entry_date"] > fs]
    stats = {}
    for rule in sorted({c["rule"] for c in closed}):
        cs = [c for c in closed if c["rule"] == rule]
        rs = [c["r_multiple"] for c in cs if c["r_multiple"] is not None]
        stats[rule] = {"trades": len(cs), "win_rate_pct": round(sum(c["pnl"] > 0 for c in cs) / len(cs) * 100, 1),
                       "avg_r": round(float(np.mean(rs)), 3) if rs else None,
                       "avg_hold_days": round(float(np.mean([c["hold_days"] for c in cs])), 1),
                       "max_loss": min(c["pnl"] for c in cs), "total_pnl": round(sum(c["pnl"] for c in cs), 0),
                       "sample_note": "樣本不足，僅供參考" if len(cs) < min_n else ""}
    out["stats_by_rule"] = stats
    out["closed_count"] = len(closed)
    out["open_count"] = sum(1 for p in rep["positions"] if p["entry_date"] > fs)
    for c in rep["closed_trades"]:
        c["forward"] = c["entry_date"] > fs
    for p in rep["positions"]:
        p["forward"] = p["entry_date"] > fs
    return out


def main():
    cfg = load_config()
    P = Panel(cfg)
    sim = Sim(P, cfg).run()
    rep = sim.report(int(cfg["backtest"]["sample_min"]))
    fwd = forward_block(rep, cfg, int(cfg["backtest"]["sample_min"]))
    out = {"generated_at": now_tw().isoformat(timespec="seconds"), "data_date": P.dates[-1],
           "notes": ["規則模擬帳戶，不做主觀判斷；與實際持股無關",
                     "訊號在收盤產生、隔日開盤成交；價格為還原價（含息），實際成交價會不同",
                     "前向測試從 config paper.forward_start 起算；之前是事後重播，規則是看過這段行情後才調整，只供參考",
                     "樣本少於 config backtest.sample_min 筆的統計標「樣本不足，僅供參考」"],
           "params": cfg["paper"], "forward": fwd, **rep}
    write_json(path("data", "paper.json"), clean(out), compact=True)
    log(f"paper.json：{rep['start_date']}~{rep['end_date']} 總報酬 {rep['total_return_pct']}% "
        f"（0050 {rep['bench_return_pct']}%）MDD {rep['max_drawdown_pct']}%；成交 {len(rep['trades'])} 筆；"
        f"持倉 {len(rep['positions'])}；統計 {rep['stats_by_rule']}")


if __name__ == "__main__":
    main()
