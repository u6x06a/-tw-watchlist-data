#!/usr/bin/env python3
"""階段 5：回測。用 paper.py 的同一套邏輯（scripts/sim.py + scripts/rules.py）跑全部歷史，寫 reports/backtest_YYYYMMDD.md。

內容：
  1. 基準情境（config 預設）：各規則與整體的交易筆數、勝率、平均 R、報酬、最大回撤，並與 0050 比較
  2. R1 依進場時籌碼標記分組：chips_confirm（有 / 無法人確認）、chips_warning
  3. 籌碼條件開關比較：L1 止穩條件 c 開 / 關（L1 需讀新聞，基準情境不執行，這裡另以「執行 L1」情境比較）
  4. 0050 大跌研究：單日跌 ≥ 3% 的日子，之後 1/3/5/10 日 0050 與 00685L 報酬、幾天收回恐慌日高點、多少比例跌破恐慌日低點
  5. 敏感度：R1 量比門檻、停損 ATR 倍數、L1 跌深門檻各 3 個值；前 2/3 期間選參數、後 1/3 期間驗證
樣本少於 config backtest.sample_min 筆的統計一律標「樣本不足，僅供參考」。不提供買賣建議。

用法：python scripts/backtest.py [--quick]   （--quick：略過敏感度分析）
"""
import copy
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, log, now_tw, path, write_json  # noqa: E402
from panel import Panel  # noqa: E402
from screen import clean  # noqa: E402
from sim import Sim  # noqa: E402


def run(P, cfg, start_i, end_i, opts=None, tweak=None):
    """以修改後的 config 跑一次模擬（tweak：[(路徑 tuple, 值)]）。P.cfg 會暫時換成修改後的版本，跑完還原。"""
    c = copy.deepcopy(cfg)
    for keys, val in tweak or []:
        d = c
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = val
    old = P.cfg
    P.cfg = c
    try:
        sim = Sim(P, c, opts=opts, start_i=start_i, end_i=end_i).run()
        return sim.report(int(c["backtest"]["sample_min"])), sim
    finally:
        P.cfg = old


def group_stats(trades, min_n):
    if not trades:
        return {"trades": 0, "note": "沒有交易"}
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {"trades": len(trades), "win_rate_pct": round(sum(t["pnl"] > 0 for t in trades) / len(trades) * 100, 1),
            "avg_r": round(float(np.mean(rs)), 3) if rs else None, "total_pnl": round(sum(t["pnl"] for t in trades), 0),
            "note": "樣本不足，僅供參考" if len(trades) < min_n else ""}


def overall(rep, min_n):
    cs = rep["closed_trades"]
    g = group_stats(cs, min_n)
    g.update({"total_return_pct": rep["total_return_pct"], "max_drawdown_pct": rep["max_drawdown_pct"],
              "bench_return_pct": rep["bench_return_pct"], "bench_max_drawdown_pct": rep["bench_max_drawdown_pct"]})
    return g


def panic_study(P, cfg):
    ps = cfg["backtest"]["panic_study"]
    b = P.bench
    c, h, l = P.C[b].values, P.H[b].values, P.L[b].values
    lev = P.C["00685L"].values if "00685L" in P.C.columns else None
    chg = np.r_[np.nan, c[1:] / c[:-1] - 1] * 100
    rows = []
    for i in np.where(chg <= -float(ps["drop_pct"]))[0]:
        r = {"date": P.dates[i], "change_pct": round(float(chg[i]), 2)}
        for n in ps["horizons"]:
            n = int(n)
            r[f"0050_{n}d"] = round(float(c[i + n] / c[i] - 1) * 100, 2) if i + n < len(c) else None
            r[f"00685L_{n}d"] = round(float(lev[i + n] / lev[i] - 1) * 100, 2) \
                if lev is not None and i + n < len(c) and not np.isnan(lev[i]) and not np.isnan(lev[i + n]) else None
        after = c[i + 1:]
        k = np.where(after > h[i])[0]
        r["days_to_reclaim_high"] = int(k[0] + 1) if len(k) else None
        w = after[:10]
        r["broke_low_within_10d"] = bool((w < l[i]).any()) if len(w) else None
        rows.append(r)
    summ = {"events": len(rows)}
    for n in ps["horizons"]:
        for name in ("0050", "00685L"):
            x = [r[f"{name}_{int(n)}d"] for r in rows if r[f"{name}_{int(n)}d"] is not None]
            summ[f"{name}_{n}d_mean"] = round(float(np.mean(x)), 2) if x else None
            summ[f"{name}_{n}d_median"] = round(float(np.median(x)), 2) if x else None
            summ[f"{name}_{n}d_positive_pct"] = round(sum(v > 0 for v in x) / len(x) * 100, 1) if x else None
    rec = [r["days_to_reclaim_high"] for r in rows if r["days_to_reclaim_high"] is not None]
    summ["reclaimed_high_pct"] = round(len(rec) / len(rows) * 100, 1) if rows else None
    summ["days_to_reclaim_median"] = float(np.median(rec)) if rec else None
    br = [r["broke_low_within_10d"] for r in rows if r["broke_low_within_10d"] is not None]
    summ["broke_low_within_10d_pct"] = round(sum(br) / len(br) * 100, 1) if br else None
    summ["note"] = "樣本不足，僅供參考" if len(rows) < int(cfg["backtest"]["sample_min"]) else ""
    return {"summary": summ, "events": rows}


def main():
    cfg = load_config()
    bt = cfg["backtest"]
    min_n = int(bt["sample_min"])
    P = Panel(cfg)
    s0, e0 = int(bt["warmup_days"]), len(P.dates) - 1
    split = s0 + int((e0 - s0) * float(bt["split_ratio"]))
    log(f"回測期間 {P.dates[s0]} ~ {P.dates[e0]}（{e0 - s0 + 1} 個交易日）；調參 {P.dates[s0]}~{P.dates[split]}，驗證之後")
    res = {"period": [P.dates[s0], P.dates[e0]], "trading_days": e0 - s0 + 1,
           "split_date": P.dates[split], "sample_min": min_n}

    base, sim = run(P, cfg, s0, e0)
    res["base"] = {"overall": overall(base, min_n), "by_rule": base["stats_by_rule"],
                   "accounts": base["accounts"], "not_executed_counts": base["not_executed_counts"]}
    r1 = [t for t in base["closed_trades"] if t["rule"] == "R1"]
    res["r1_chips"] = {"chips_confirm=true": group_stats([t for t in r1 if t["chips_confirm"] is True], min_n),
                       "chips_confirm=false": group_stats([t for t in r1 if t["chips_confirm"] is False], min_n),
                       "chips_confirm=null（無籌碼資料）": group_stats([t for t in r1 if t["chips_confirm"] is None], min_n),
                       "chips_warning=true": group_stats([t for t in r1 if t["chips_warning"] is True], min_n),
                       "chips_warning=false": group_stats([t for t in r1 if t["chips_warning"] is False], min_n)}
    log("基準情境完成")
    l1_exec = [(("paper", "execute", "needs_news_check"), True), (("paper", "execute", "L2"), False)]
    on, _ = run(P, cfg, s0, e0, opts={"rules": {"use_chips_l1c": True}}, tweak=l1_exec)
    off, _ = run(P, cfg, s0, e0, opts={"rules": {"use_chips_l1c": False}}, tweak=l1_exec)
    res["chips_toggle_l1"] = {"L1 執行、止穩條件 c 使用籌碼": on["stats_by_rule"].get("L1", {"trades": 0}),
                              "L1 執行、止穩條件 c 關閉（a、b 都要）": off["stats_by_rule"].get("L1", {"trades": 0}),
                              "note": "L1 需讀新聞確認，模擬帳戶預設不執行；此比較假設全部執行（未經新聞判斷）"}
    log("籌碼開關比較完成")
    res["panic_study"] = panic_study(P, cfg)

    if "--quick" not in sys.argv:
        sens = {}
        grid = bt["sensitivity"]
        specs = {"r1_vol_mult": (("screen", "r1", "vol_mult"), "R1", []),
                 "r1_stop_atr_mult": (("screen", "r1", "stop_atr_mult"), "R1", []),
                 "l1_deep_atr_mult": (("screen", "l1", "deep_atr_mult"), "L1", l1_exec)}
        for name, (keys, rule, extra) in specs.items():
            rows = []
            for v in grid[name]:
                ins, _ = run(P, cfg, s0, split, tweak=extra + [(keys, v)])
                oos, _ = run(P, cfg, split + 1, e0, tweak=extra + [(keys, v)])
                rows.append({"value": v, "in_sample": ins["stats_by_rule"].get(rule, {"trades": 0}),
                             "out_of_sample": oos["stats_by_rule"].get(rule, {"trades": 0})})
                log(f"敏感度 {name}={v} 完成")
            scored = [r for r in rows if r["in_sample"].get("avg_r") is not None]
            best = max(scored, key=lambda r: r["in_sample"]["avg_r"])["value"] if scored else None
            sens[name] = {"rule": rule, "default": _get(cfg, keys), "best_in_sample_by_avg_r": best, "rows": rows}
        res["sensitivity"] = sens
    res["disabled_rules"] = {"C1": "停用：集保股權分散只能從開始執行起每週累積，未滿 config chips.tdcc_rules.min_weeks 週",
                             "big_holder_increase": "同上"}
    stamp = now_tw().strftime("%Y%m%d")
    write_json(path("reports", f"backtest_{stamp}.json"), clean(res))
    write_md(res, path("reports", f"backtest_{stamp}.md"), cfg)
    log(f"寫入 reports/backtest_{stamp}.md")


def _get(cfg, keys):
    d = cfg
    for k in keys:
        d = d[k]
    return d


def _t(st):
    if not st or not st.get("trades"):
        return "0 筆", "-", "-", "-"
    return (f"{st['trades']} 筆" + ("（樣本不足，僅供參考）" if st.get("note") or st.get("sample_note") else ""),
            f"{st.get('win_rate_pct')}%", f"{st.get('avg_r')}", f"{st.get('total_pnl')}")


def write_md(res, out, cfg):
    L = [f"# 規則回測報告（{res['period'][0]} ~ {res['period'][1]}）", "",
         f"產生時間：{now_tw():%Y-%m-%d %H:%M}。資料：還原價（含息），訊號收盤產生、隔日開盤成交，含手續費與證交稅。",
         f"回測 {res['trading_days']} 個交易日；樣本少於 {res['sample_min']} 筆的統計標「樣本不足，僅供參考」，"
         "不可解讀為規律。本報告只描述規則在歷史資料上的表現，不構成買賣建議。", "",
         "## 1. 基準情境（config 預設）", ""]
    o = res["base"]["overall"]
    L += ["| 項目 | 規則帳戶（個股 + ETF） | 0050 買進持有 |", "|---|---|---|",
          f"| 總報酬 | {o['total_return_pct']}% | {o['bench_return_pct']}% |",
          f"| 最大回撤 | {o['max_drawdown_pct']}% | {o['bench_max_drawdown_pct']}% |",
          f"| 已結束交易 | {o['trades']} 筆 {o.get('note', '')} | - |", ""]
    L += ["| 規則 | 筆數 | 勝率 | 平均 R | 平均持有天數 | 最大虧損 | 損益合計 |", "|---|---|---|---|---|---|---|"]
    for rule, st in res["base"]["by_rule"].items():
        L.append(f"| {rule} | {st['trades']} {st['sample_note']} | {st['win_rate_pct']}% | {st['avg_r']} | "
                 f"{st['avg_hold_days']} | {st['max_loss']} | {st['total_pnl']} |")
    L += ["", "未執行的訊號（只記錄）：", ""] + [f"- {k}：{v}" for k, v in res["base"]["not_executed_counts"].items()]
    L += ["", "## 2. R1 依籌碼標記分組", "",
          "外資買賣超可能包含 ETF 被動調整與避險，單日數字有限制；此處只比較進場當時的標記與結果。", "",
          "| 分組 | 筆數 | 勝率 | 平均 R | 損益合計 |", "|---|---|---|---|---|"]
    for k, st in res["r1_chips"].items():
        a, b_, c_, d_ = _t(st)
        L.append(f"| {k} | {a} | {b_} | {c_} | {d_} |")
    L += ["", "## 3. 籌碼條件開關（L1 止穩條件 c）", "", res["chips_toggle_l1"]["note"], "",
          "| 情境 | 筆數 | 勝率 | 平均 R | 損益合計 |", "|---|---|---|---|---|"]
    for k, st in res["chips_toggle_l1"].items():
        if k == "note":
            continue
        a, b_, c_, d_ = _t(st)
        L.append(f"| {k} | {a} | {b_} | {c_} | {d_} |")
    ps = res["panic_study"]["summary"]
    L += ["", f"## 4. 0050 單日跌 ≥ {cfg['backtest']['panic_study']['drop_pct']}% 之後", "",
          f"事件數：{ps['events']} {ps['note']}", "",
          "| 天數 | 0050 平均 | 0050 中位數 | 0050 上漲比例 | 00685L 平均 | 00685L 中位數 | 00685L 上漲比例 |",
          "|---|---|---|---|---|---|---|"]
    for n in cfg["backtest"]["panic_study"]["horizons"]:
        L.append(f"| {n} 日 | {ps[f'0050_{n}d_mean']}% | {ps[f'0050_{n}d_median']}% | {ps[f'0050_{n}d_positive_pct']}% | "
                 f"{ps[f'00685L_{n}d_mean']}% | {ps[f'00685L_{n}d_median']}% | {ps[f'00685L_{n}d_positive_pct']}% |")
    L += ["", f"- 收回恐慌日高點的比例：{ps['reclaimed_high_pct']}%，所需交易日中位數：{ps['days_to_reclaim_median']}",
          f"- 10 日內跌破恐慌日低點的比例：{ps['broke_low_within_10d_pct']}%", "",
          "| 日期 | 當日 | 0050 1/3/5/10 日 | 00685L 1/3/5/10 日 | 收回高點天數 | 10 日內破低 |", "|---|---|---|---|---|---|"]
    for r in res["panic_study"]["events"]:
        hs = cfg["backtest"]["panic_study"]["horizons"]
        L.append(f"| {r['date']} | {r['change_pct']}% | {' / '.join(str(r[f'0050_{n}d']) for n in hs)} | "
                 f"{' / '.join(str(r[f'00685L_{n}d']) for n in hs)} | {r['days_to_reclaim_high']} | {r['broke_low_within_10d']} |")
    if "sensitivity" in res:
        L += ["", "## 5. 敏感度（前 2/3 期間選參數、後 1/3 期間驗證）", "",
              f"調參期間至 {res['split_date']}，之後為驗證期間。只比較 3 個參數，各 3 個值；以調參期間平均 R 選出的值，"
              "要看它在驗證期間是否仍維持，不能只看調參期間。", ""]
        for name, s in res["sensitivity"].items():
            L += [f"### {name}（規則 {s['rule']}；目前設定 {s['default']}；調參期間平均 R 最佳 {s['best_in_sample_by_avg_r']}）", "",
                  "| 值 | 調參期間 筆數 / 勝率 / 平均 R | 驗證期間 筆數 / 勝率 / 平均 R |", "|---|---|---|"]
            for r in s["rows"]:
                a = _t(r["in_sample"])
                b_ = _t(r["out_of_sample"])
                L.append(f"| {r['value']} | {a[0]} / {a[1]} / {a[2]} | {b_[0]} / {b_[1]} / {b_[2]} |")
            L.append("")
    L += ["## 6. 停用的規則", ""] + [f"- {k}：{v}" for k, v in res["disabled_rules"].items()]
    L += ["", "## 限制", "",
          "- 回測期間短（約一年），多數分組樣本不足，數字只能當作規則行為的檢查，不能當作績效預期。",
          "- 月營收的公布日：opendata 有出表日期就用；歷史月份以法定期限（次月 10 日）推估，可能比實際公布晚。",
          "- 外資持股、融資融券常晚一個交易日公布；回測以當日已有的資料為準，缺資料的條件視為不成立或略過。",
          "- 還原價含息，與實際成交價不同；手續費以 config 設定計算（最低手續費會放大小部位的成本）。"]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
