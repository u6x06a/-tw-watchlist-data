#!/usr/bin/env python3
"""階段 2：每日指標。輸出 data/indicators.json（全部台股普通股與 ETF）與 data/market.json（大盤狀態）。

價格一律用還原價（scripts/adjlib.load_adjusted：prices.csv + adj_factors.csv 即時計算）；所有參數在 config/rules.yaml
的 indicators / chips_indicators / market 區塊。所有數字由本腳本計算，每檔與每個區塊都帶資料日期。

indicators.json
  generated_at, data_date（台股最新交易日）, market_status（meta.json markets 摘要）, params
  stocks:     {代號: 每檔指標}（見 stock_metrics() 的欄位說明）
  industries: {產業: 5/20 日報酬中位數、RS 中位數、排名}
  indicators: 舊版區塊（關注名單 + 海外代號，格式不變；scripts/indicators_legacy.py）
market.json：0050 大盤狀態、543 階梯、恐慌日、追蹤日、全市場法人與融資、結論（正常 / 過濾中）

用法：python scripts/indicators.py
"""
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adjlib import load_adjusted  # noqa: E402
from common import CODE_OK, load_config, log, now_tw, path, read_json, write_json  # noqa: E402

OUT = path("data", "indicators.json")
MARKET_OUT = path("data", "market.json")


def rnd(x, n=4):
    if x is None:
        return None
    try:
        if np.isnan(x):
            return None
    except TypeError:
        pass
    return round(float(x), n)


def pct(a, b, n=2):
    if a is None or b is None or b == 0 or np.isnan(a) or np.isnan(b):
        return None
    return round((a / b - 1) * 100, n)


def sma(x, n, end=None):
    """x 的最後 n 根平均（end：倒數第幾根結束，0 = 最新）。根數不足回傳 None。"""
    end = end or 0
    if len(x) < n + end:
        return None
    seg = x[len(x) - n - end: len(x) - end]
    return float(np.mean(seg))


def wilder_atr(h, l, c, n):
    if len(c) < n + 1:
        return None
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr = tr[:n].mean()
    for v in tr[n:]:
        atr = (atr * (n - 1) + v) / n
    return float(atr)


def ret(c, n):
    return None if len(c) <= n else float(c[-1] / c[-1 - n] - 1)


def streak(vals):
    """由新到舊連續同號天數：+3 = 連 3 日買超、-2 = 連 2 日賣超、0 = 最新一日為 0 或無資料。"""
    s = 0
    for v in reversed(vals):
        if v is None or np.isnan(v) or v == 0:
            break
        if s == 0:
            s = 1 if v > 0 else -1
        elif (v > 0) == (s > 0):
            s += 1 if s > 0 else -1
        else:
            break
    return s


# ---------------------------------------------------------------- 讀資料
def load_inputs():
    comp = pd.read_csv(path("data", "companies.csv"), dtype=str, keep_default_na=False) \
        if os.path.exists(path("data", "companies.csv")) else pd.DataFrame(columns=["code"])
    comp = comp.set_index("code")

    def opt(name):
        p = path("data", f"{name}.csv")
        return pd.read_csv(p, dtype={"code": str, "date": str}) if os.path.exists(p) else None
    rev = pd.read_csv(path("data", "revenue.csv"), dtype={"code": str, "ym": str}) \
        if os.path.exists(path("data", "revenue.csv")) else None
    tdcc = pd.read_csv(path("data", "tdcc.csv"), dtype={"code": str, "date": str}) \
        if os.path.exists(path("data", "tdcc.csv")) else None
    return comp, opt("institutional"), opt("foreign_holding"), opt("margin"), rev, tdcc


def group(df, key="code"):
    if df is None or not len(df):
        return {}
    sk = "date" if "date" in df.columns else "ym"
    return {k: g.sort_values(sk) for k, g in df.groupby(key)}


# ---------------------------------------------------------------- 個股
def stock_metrics(g, cfg, bench, rs_score_of):
    """g：單一代號的還原價（依日期排序）。回傳 dict（不含 RS 排名、產業、籌碼、營收，之後補上）。"""
    ci = cfg["indicators"]
    c, h, l = g["close"].values, g["high"].values, g["low"].values
    v = g["volume"].values.astype(float)
    raw_c = g["raw_close"].values
    dates = g["date"].values
    n = len(c)
    it = {"data_date": dates[-1], "bars": n, "close": rnd(c[-1]), "raw_close": rnd(raw_c[-1])}
    lb = int(ci["slope_lookback"])
    for p in ci["ma_periods"]:
        m0, m1 = sma(c, p), sma(c, p, lb)
        it[f"ma{p}"] = rnd(m0)
        it[f"ma{p}_slope"] = rnd(None if m0 is None or m1 is None else m0 - m1)
        it[f"ma{p}_slope_pct"] = pct(m0, m1) if m0 is not None and m1 is not None else None
    atr = wilder_atr(h, l, c, int(ci["atr_period"]))
    it["atr14"] = rnd(atr)
    it["atr14_pct"] = rnd(atr / c[-1] * 100, 2) if atr else None
    a5, a20 = sma(v, 5), sma(v, 20)
    a5_prev = sma(v, 5, 1)
    it["vol"] = rnd(v[-1], 0)
    it["vol_ratio_5"] = rnd(v[-1] / a5, 3) if a5 else None
    it["vol_ratio_20"] = rnd(v[-1] / a20, 3) if a20 else None
    it["vol_5_20"] = rnd(a5 / a20, 3) if a5 and a20 else None
    it["vol_ma5"], it["vol_ma20"], it["vol_ma5_prev"] = rnd(a5, 0), rnd(a20, 0), rnd(a5_prev, 0)
    it["vol_ma20_prev"] = rnd(sma(v, 20, 1), 0)
    raw_v = g["raw_volume"].values.astype(float)
    it["avg_value_20"] = rnd(float(np.mean(raw_c[-20:] * raw_v[-20:])), 0) if n >= 20 else None
    it["vol_inconsistent_dates"] = [d for d, f in zip(dates[-20:], g["vol_fallback"].values[-20:]) if f]
    it["adj_uncertain_dates"] = [d for d, f in zip(dates[-60:], g["adj_uncertain"].values[-60:]) if f]
    # 52 週
    k = int(ci["hl52_days"])
    if n >= k:
        hi, lo = float(np.max(h[-k:])), float(np.min(l[-k:]))
        it.update({"high_52w": rnd(hi), "low_52w": rnd(lo), "from_high_52w_pct": pct(c[-1], hi),
                   "from_low_52w_pct": pct(c[-1], lo), "hl52_status": "ok"})
    else:
        it.update({"high_52w": None, "low_52w": None, "from_high_52w_pct": None, "from_low_52w_pct": None,
                   "hl52_status": f"insufficient（{n}/{k} 根）"})
    # 報酬與相對 0050
    for p in sorted(set(ci["rel_periods"]) | {int(x) for x in ci["rs_rank"]["weights"]} | {1, 10}):
        r = ret(c, p)
        it[f"ret{p}_pct"] = rnd(r * 100, 2) if r is not None else None
    it["rel_0050"] = {}
    if bench is not None:
        bd, bc = bench
        for p in ci["rel_periods"]:
            if n <= p:
                continue
            s_d, e_d = dates[-1 - p], dates[-1]
            i0, i1 = np.searchsorted(bd, s_d, "right") - 1, np.searchsorted(bd, e_d, "right") - 1
            if i0 < 0 or i1 < 0:
                continue
            rb = bc[i1] / bc[i0] - 1
            it["rel_0050"][f"{p}d"] = rnd((c[-1] / c[-1 - p] - 1 - rb) * 100, 2)
            if bd[i0] != s_d or bd[i1] != e_d:
                it["rel_0050"][f"{p}d_dates_aligned"] = False
    # RS 分數（排名之後整體計算）
    w = ci["rs_rank"]["weights"]
    rs = [ret(c, int(p)) for p in w]
    it["_rs_score"] = None if any(x is None for x in rs) else sum(float(wt) * x for wt, x in zip(w.values(), rs))
    # 階段（Weinstein 精簡版）
    st = ci["stage"]
    m_now, m_prev = sma(c, int(st["ma"])), sma(c, int(st["ma"]), int(st["slope_lookback"]))
    m_prior = sma(c, int(st["ma"]), int(st["prior_lookback"]))
    m_prior_prev = sma(c, int(st["ma"]), int(st["prior_lookback"]) + int(st["slope_lookback"]))
    if None in (m_now, m_prev):
        it["stage"], it["stage_basis"] = None, "insufficient"
    else:
        slope = (m_now / m_prev - 1) * 100
        flat = abs(slope) <= float(st["flat_pct"])
        above = c[-1] > m_now
        if not flat and slope > 0:
            stage = "上漲" if above else "做頭"
        elif not flat and slope < 0:
            stage = "下跌" if not above else "打底"
        else:
            prior = None if None in (m_prior, m_prior_prev) else (m_prior / m_prior_prev - 1) * 100
            if prior is None:
                stage = "打底" if not above else "做頭"
            else:
                stage = "做頭" if prior > 0 else "打底"
        it["stage"] = stage
        it["stage_basis"] = {"ma150_slope_pct": rnd(slope, 2), "close_above_ma150": bool(above)}
    return it


def trend_template(it, cfg):
    t = cfg["indicators"]["trend_template"]
    c, m60, m150, m240 = it["close"], it.get("ma60"), it.get("ma150"), it.get("ma240")
    checks = {}

    def chk(name, cond):
        checks[name] = None if cond is None else bool(cond)
    chk("close>ma150>ma240", None if None in (m150, m240) else c > m150 > m240)
    lb = int(t["ma240_rising_lookback"])
    chk(f"ma240_rising_{lb}d", it.get("_ma240_prev") if it.get("_ma240_prev") is None
        else (m240 is not None and m240 > it["_ma240_prev"]))
    chk("ma60>ma150", None if None in (m60, m150) else m60 > m150)
    chk("close>ma60", None if m60 is None else c > m60)
    chk(f"above_52w_low>={t['min_above_52w_low_pct']}%",
        None if it["from_low_52w_pct"] is None else it["from_low_52w_pct"] >= float(t["min_above_52w_low_pct"]))
    chk(f"within_{t['max_below_52w_high_pct']}%_of_52w_high",
        None if it["from_high_52w_pct"] is None else it["from_high_52w_pct"] >= -float(t["max_below_52w_high_pct"]))
    chk(f"rs_rank>={t['min_rs_rank']}", None if it.get("rs_rank") is None else it["rs_rank"] >= float(t["min_rs_rank"]))
    vals = list(checks.values())
    passed = None if any(v is None for v in vals) else all(vals)
    return {"pass": passed, "checks": checks,
            "note": "資料不足的項目為 null，整體亦為 null" if passed is None else ""}


# ---------------------------------------------------------------- 籌碼
def chips_block(code, it, ig, fg, mg, tg, comp_row, cfg, quarter_end):
    cc = cfg["chips_indicators"]
    out = {}
    vol_ma20_lots = (it["vol_ma20"] or 0) / 1000 or None
    if ig is not None and len(ig):
        f = (ig["foreign_net"].astype(float) + ig["foreign_dealer_net"].fillna(0).astype(float)).values / 1000
        tr = ig["trust_net"].astype(float).values / 1000
        blk = {"data_date": ig["date"].values[-1]}
        for w in cc["net_windows"]:
            for name, arr in (("foreign", f), ("trust", tr)):
                if len(arr) >= w:
                    s = float(np.nansum(arr[-w:]))
                    blk[f"{name}_net_{w}d_lots"] = rnd(s, 1)
                    blk[f"{name}_net_{w}d_pct_vol20"] = rnd(s / vol_ma20_lots * 100, 2) if vol_ma20_lots else None
        blk["foreign_streak"] = streak(list(f))
        blk["trust_streak"] = streak(list(tr))
        ta = cc["trust_adopt"]
        shares = float(comp_row.get("shares_outstanding") or 0) if comp_row is not None else 0
        tw_ = int(ta["window"])
        if shares and len(tr) >= tw_:
            p = float(np.nansum(tr[-tw_:])) * 1000 / shares * 100
            blk[f"trust_net_{tw_}d_pct_shares"] = rnd(p, 3)
            blk["trust_adopt"] = p >= float(ta["min_pct_of_shares"])
        blk["window_dressing_possible"] = bool(quarter_end and blk.get("trust_net_5d_lots", 0) and
                                               blk.get("trust_net_5d_lots", 0) > 0)
        out["institutional"] = blk
    if fg is not None and len(fg):
        w = int(cc["foreign_pct_change_window"])
        fp = fg["foreign_pct"].astype(float).values
        out["foreign_holding"] = {"data_date": fg["date"].values[-1], "foreign_pct": rnd(fp[-1], 3),
                                  f"foreign_pct_chg_{w}d": rnd(fp[-1] - fp[-1 - w], 3) if len(fp) > w else None}
    if mg is not None and len(mg):
        mb = mg["margin_balance"].astype(float).values
        sb = mg["short_balance"].astype(float).values
        lim = mg["margin_limit"].astype(float).values
        sbl = mg["sbl_short_balance"].astype(float).values
        blk = {"data_date": mg["date"].values[-1], "margin_balance_lots": rnd(mb[-1], 0),
               "short_balance_lots": rnd(sb[-1], 0)}
        for w in cc["margin_change_windows"]:
            blk[f"margin_chg_{w}d_pct"] = pct(mb[-1], mb[-1 - w]) if len(mb) > w and mb[-1 - w] else None
        blk["margin_use_pct"] = rnd(mb[-1] / lim[-1] * 100, 2) if lim[-1] and not np.isnan(lim[-1]) else None
        blk["short_margin_ratio_pct"] = rnd(sb[-1] / mb[-1] * 100, 2) if mb[-1] else None
        w = int(cc["sbl_change_window"])
        if len(sbl) > w and not np.isnan(sbl[-1]) and not np.isnan(sbl[-1 - w]):
            blk[f"sbl_chg_{w}d_shares"] = rnd(sbl[-1] - sbl[-1 - w], 0)
            blk[f"sbl_chg_{w}d_pct"] = pct(sbl[-1], sbl[-1 - w]) if sbl[-1 - w] else None
        blk["_mb"] = mb
        out["margin"] = blk
    if tg is not None and len(tg):
        p = tg["pct_1000plus"].astype(float).values
        w = int(cc["tdcc_change_weeks"])
        out["tdcc"] = {"data_date": tg["date"].values[-1], "weeks_available": len(p), "pct_1000plus": rnd(p[-1], 3),
                       "pct_400plus": rnd(float(tg["pct_400plus"].values[-1]), 3),
                       f"pct_1000plus_chg_{w}w": rnd(p[-1] - p[-1 - w], 3) if len(p) > w else None,
                       "_p": p}
    return out


def chips_states(it, ch, cfg):
    s = cfg["chips_indicators"]["states"]
    tdr = cfg["chips"]["tdcc_rules"]
    out = {}
    ins = ch.get("institutional") or {}
    w = int(s["sync_buy"]["window"])
    f5, t5 = ins.get(f"foreign_net_{w}d_lots"), ins.get(f"trust_net_{w}d_lots")
    out["sync_buy"] = None if f5 is None or t5 is None else (f5 > 0 and t5 > 0)
    mg = ch.get("margin") or {}
    mb = mg.get("_mb")
    rk = s["retail_catch_knife"]
    w = int(rk["window"])
    r = it.get(f"ret{w}_pct")
    if mb is not None and len(mb) > w and r is not None and mb[-1 - w]:
        chg = (mb[-1] / mb[-1 - w] - 1) * 100
        out["retail_catch_knife"] = bool(r < 0 and chg >= float(rk["min_margin_increase_pct"]))
    else:
        out["retail_catch_knife"] = None
    wo = s["washout"]
    w = int(wo["window"])
    r = it.get(f"ret{w}_pct")
    if mb is not None and len(mb) > w and r is not None and mb[-1 - w] and it.get("vol_5_20") is not None:
        chg = (mb[-1] / mb[-1 - w] - 1) * 100
        out["washout"] = bool(r < 0 and chg <= -float(wo["min_margin_decrease_pct"])
                              and it["vol_5_20"] < float(wo["max_vol_ratio_5_20"]))
    else:
        out["washout"] = None
    td = ch.get("tdcc") or {}
    p = td.get("_p")
    weeks = int(s["big_holder_increase"]["weeks"])
    if not tdr.get("big_holder_increase_enabled"):
        out["big_holder_increase"] = None
        out["big_holder_increase_note"] = f"停用：集保資料需累積 {tdr['min_weeks']} 週（目前 {td.get('weeks_available', 0)} 週）"
    elif p is not None and len(p) > weeks:
        out["big_holder_increase"] = bool(all(p[-i] > p[-i - 1] for i in range(1, weeks + 1)))
    else:
        out["big_holder_increase"] = None
    return out


# ---------------------------------------------------------------- 營收
def revenue_block(rg):
    if rg is None or not len(rg):
        return None
    rg = rg.sort_values("ym")
    last = rg.iloc[-1]
    revs = rg["revenue_k"].astype(float).values
    hi12 = bool(len(revs) >= 12 and revs[-1] >= np.nanmax(revs[-12:]))
    yoy3 = rg["yoy_pct"].astype(float).values[-3:]
    return {"ym": last["ym"], "revenue_k": rnd(last["revenue_k"], 0), "yoy_pct": rnd(last["yoy_pct"], 2),
            "mom_pct": rnd(last["mom_pct"], 2), "high_12m": hi12 if len(revs) >= 12 else None,
            "months_available": int(len(revs)), "yoy_3m_avg": rnd(float(np.nanmean(yoy3)), 2) if len(yoy3) == 3 else None,
            "source_date": last.get("source_date")}


# ---------------------------------------------------------------- 大盤
def market_block(bg, inst, margin, cfg, meta):
    mc = cfg["market"]
    c, h, l = bg["close"].values, bg["high"].values, bg["low"].values
    v = bg["raw_volume"].values.astype(float)
    d = bg["date"].values
    out = {"bench": mc["bench"], "data_date": d[-1], "close": rnd(c[-1]),
           "ma20": rnd(sma(c, int(mc["ma_short"]))), "ma60": rnd(sma(c, int(mc["ma_long"])))}
    m60, m60p = sma(c, int(mc["ma_long"])), sma(c, int(mc["ma_long"]), int(mc["slope_lookback"]))
    out["above_ma60"] = bool(c[-1] > m60) if m60 else None
    out["ma60_slope"] = rnd(m60 - m60p) if m60 and m60p else None
    out["ma60_slope_pct"] = pct(m60, m60p) if m60 and m60p else None
    out["change_pct"] = pct(c[-1], c[-2])
    out["range_pct"] = rnd((h[-1] - l[-1]) / c[-2] * 100, 2)
    hl = int(mc["high_lookback"])
    i_hi = len(c) - hl + int(np.argmax(c[-hl:]))
    out["high_60d"] = {"date": d[i_hi], "close": rnd(c[i_hi])}
    out["drawdown_from_60d_high_pct"] = pct(c[-1], c[i_hi])
    # 543 階梯：自近 60 日高點以來
    lad = mc["ladder"]
    chg = np.r_[np.nan, c[1:] / c[:-1] - 1] * 100
    steps = []
    for i in range(i_hi + 1, len(c)):
        if chg[i] <= -float(lad["daily_drop_pct"]):
            steps.append({"type": f"daily_drop>={lad['daily_drop_pct']}%", "date": d[i], "change_pct": rnd(chg[i], 2)})
    for sp in lad["drawdown_steps_pct"]:
        for i in range(i_hi + 1, len(c)):
            if (c[i] / c[i_hi] - 1) * 100 <= -float(sp):
                steps.append({"type": f"drawdown>={sp}%", "date": d[i], "drawdown_pct": pct(c[i], c[i_hi])})
                break
    steps.sort(key=lambda x: x["date"])
    for k, s in enumerate(steps, 1):
        s["level"] = k
    out["ladder"] = {"since_high_date": d[i_hi], "level": len(steps), "steps": steps,
                     "new_step_today": [s for s in steps if s["date"] == d[-1]]}
    # 恐慌日
    pc = mc["panic"]
    lbk = int(pc["lookback_days"])
    idx = [i for i in range(max(1, len(c) - lbk), len(c)) if chg[i] <= -float(pc["daily_drop_pct"])]
    if idx:
        i = idx[-1]
        after_c = c[i + 1:]
        reclaimed = [d[i + 1 + k] for k, x in enumerate(after_c) if x > h[i]]
        broke = [d[i + 1 + k] for k, x in enumerate(after_c) if x < l[i]]
        out["panic_day"] = {"date": d[i], "change_pct": rnd(chg[i], 2), "high": rnd(h[i]), "low": rnd(l[i]),
                            "days_since": len(c) - 1 - i, "reclaimed_high": bool(reclaimed),
                            "reclaimed_high_date": reclaimed[0] if reclaimed else None,
                            "broke_low": bool(broke), "broke_low_date": broke[0] if broke else None,
                            "currently_below_low": bool(c[-1] < l[i])}
    else:
        out["panic_day"] = None
    # 追蹤日：近 N 日最低收盤之後
    ft = mc["follow_through"]
    lbl = int(ft["low_lookback"])
    i_lo = len(c) - lbl + int(np.argmin(c[-lbl:]))
    ftd = None
    for i in range(i_lo + int(ft["min_day"]), len(c)):
        if chg[i] >= float(ft["min_gain_pct"]) and v[i] > v[i - 1]:
            ftd = {"date": d[i], "day_from_low": i - i_lo, "change_pct": rnd(chg[i], 2)}
            break
    undercut = bool(np.min(c[i_lo + 1:]) < c[i_lo]) if i_lo + 1 < len(c) else False
    out["follow_through"] = {"low_date": d[i_lo], "low_close": rnd(c[i_lo]), "rally_day": len(c) - 1 - i_lo,
                             "ftd": ftd, "low_undercut_after": undercut}
    # 全市場法人
    if inst is not None:
        mk = inst[(inst["code"] == "_MARKET") & (inst["market"] == "TWSE")].sort_values("date")
        if len(mk):
            fnet = (mk["foreign_net"].astype(float) + mk["foreign_dealer_net"].fillna(0).astype(float)).values
            tnet = mk["trust_net"].astype(float).values
            out["institutional_twse"] = {"data_date": mk["date"].values[-1], "unit": "TWD",
                                         "foreign_net": rnd(fnet[-1], 0), "trust_net": rnd(tnet[-1], 0),
                                         "foreign_streak": streak(list(fnet)), "trust_streak": streak(list(tnet)),
                                         "days_available": int(len(mk))}
    if margin is not None:
        mk = margin[margin["code"] == "_MARKET"].copy()
        if len(mk):
            tot = mk.groupby("date")["margin_value_k"].sum(min_count=2).dropna().sort_index()
            w = int(mc["margin_change_window"])
            out["margin_total"] = {"data_date": tot.index[-1] if len(tot) else None, "unit": "千元（上市 + 上櫃融資金額）",
                                   "value_k": rnd(tot.values[-1], 0) if len(tot) else None,
                                   f"chg_{w}d_pct": pct(tot.values[-1], tot.values[-1 - w]) if len(tot) > w else None,
                                   "days_available": int(len(tot))}
    # 結論
    reasons = []
    if out["above_ma60"] is False:
        reasons.append("收盤在季線（MA60）下")
    pdy = out.get("panic_day")
    if pdy and pdy["currently_below_low"]:
        reasons.append(f"收盤低於恐慌日（{pdy['date']}）低點")
    out["status"] = "過濾中" if reasons else "正常"
    out["status_reasons"] = reasons
    ms = (meta.get("markets") or {})
    tw = ms.get("TWSE") or ms.get("TW") or {}
    out["market_data_status"] = {"status": tw.get("status"), "expected_latest": tw.get("expected_latest"),
                                 "data_matches_expected": tw.get("data_latest") == d[-1] == tw.get("expected_latest")}
    return out


def strip_private(x):
    """移除以 _ 開頭的暫存欄位。"""
    if isinstance(x, dict):
        return {k: strip_private(v) for k, v in x.items() if not str(k).startswith("_")}
    return x


def main():
    cfg = load_config()
    ci = cfg["indicators"]
    meta = read_json(path("data", "meta.json"), {}) or {}
    comp, inst, fh, mg, rev, tdcc = load_inputs()
    df = load_adjusted()
    raw = pd.read_csv(path("data", "prices.csv"), dtype={"code": str, "date": str}, usecols=["code", "date", "volume"])
    df = df.merge(raw.rename(columns={"volume": "raw_volume"}), on=["code", "date"], how="left")
    partial = meta.get("partial_bars") or {}
    if partial:
        bad = {(c, d) for c, ds in partial.items() for d in ds}
        df = df[~pd.Series(list(zip(df["code"], df["date"])), index=df.index).isin(bad)]
    is_tw = df["code"].map(lambda c: bool(CODE_OK.match(c)))
    tw = df[is_tw]
    data_date = tw["date"].max()
    bench_g = tw[tw["code"] == ci["bench"]].sort_values("date")
    bench = (bench_g["date"].values, bench_g["close"].values) if len(bench_g) else None
    universe = set(ci["universe"])
    ig, fg, mgg, tg = group(inst[inst["code"] != "_MARKET"] if inst is not None else None), group(fh), \
        group(mg[mg["code"] != "_MARKET"] if mg is not None else None), group(tdcc)
    rg = group(rev) if rev is not None else {}
    month = int(data_date[5:7])
    tds = sorted(tw["date"].unique())
    month_tds = [x for x in tds if x[:7] == data_date[:7]]
    # 季底窗口：季底月份，且距月底（以日曆推估剩餘平日）≤ N 個交易日
    import calendar as _cal
    y, m = int(data_date[:4]), month
    last_day = date(y, m, _cal.monthrange(y, m)[1])
    remaining = int(np.busday_count(date.fromisoformat(data_date), last_day)) + 1
    quarter_end = month in (3, 6, 9, 12) and remaining <= int(ci["quarter_end_window_days"])
    _ = month_tds

    stocks = {}
    for code, g in tw.groupby("code"):
        crow = comp.loc[code] if code in comp.index else None
        stype = crow["sec_type"] if crow is not None else None
        if stype not in universe:
            continue
        g = g.sort_values("date")
        it = stock_metrics(g, cfg, bench, None)
        it["name"] = crow["name"] if crow is not None else ""
        it["market"] = crow["market"] if crow is not None else ""
        it["sec_type"] = stype
        it["is_etf"] = stype.startswith("etf")
        it["industry"] = (crow["industry"] or None) if crow is not None else None
        it["full_delivery"] = (crow["full_delivery"] or None) if crow is not None else None
        sh = float(crow["shares_outstanding"]) if crow is not None and crow["shares_outstanding"] else None
        it["market_cap"] = rnd(it["raw_close"] * sh, 0) if sh else None
        it["status"] = "ok" if it["data_date"] == data_date else "stale"
        if it["bars"] < 60:
            it["status"] = "insufficient" if it["status"] == "ok" else it["status"]
        it["_ma240_prev"] = sma(g["close"].values, 240, int(ci["trend_template"]["ma240_rising_lookback"]))
        ch = chips_block(code, it, ig.get(code), fg.get(code), mgg.get(code), tg.get(code), crow, cfg, quarter_end)
        if ch:
            ch["states"] = chips_states(it, ch, cfg)
            it["chips"] = ch
        rb = revenue_block(rg.get(code))
        if rb:
            it["revenue"] = rb
        stocks[code] = it

    # RS 排名（百分位）
    rs_univ = set(ci["rs_rank"]["universe"])
    base = np.array(sorted(v["_rs_score"] for v in stocks.values()
                           if v["_rs_score"] is not None and v["sec_type"] in rs_univ))
    for v in stocks.values():
        s = v["_rs_score"]
        v["rs_rank"] = rnd(np.searchsorted(base, s, "right") / len(base) * 100, 1) if s is not None and len(base) else None
        v["rs_score"] = rnd(s * 100, 2) if s is not None else None
    for v in stocks.values():
        v["trend_template"] = trend_template(v, cfg)

    # 產業
    ind = {}
    rows = [(v["industry"], v) for v in stocks.values() if v["industry"] and v["sec_type"] == "stock"
            and v["status"] == "ok"]
    for name in sorted({r[0] for r in rows}):
        mem = [v for n_, v in rows if n_ == name]

        def med(k):
            x = [m[k] for m in mem if m.get(k) is not None]
            return rnd(float(np.median(x)), 2) if x else None
        ind[name] = {"members": len(mem), "ret5_median": med("ret5_pct"), "ret20_median": med("ret20_pct"),
                     "ret1_median": med("ret1_pct"), "rs_rank_median": med("rs_rank")}
    key = ci["industry"]["rank_by"]
    ranked = sorted([k for k, v in ind.items() if v["members"] >= int(ci["industry"]["min_members"])
                     and v[key] is not None], key=lambda k: -ind[k][key])
    for i, k in enumerate(ranked, 1):
        ind[k]["rank"] = i
    for k in ind:
        ind[k].setdefault("rank", None)
    for v in stocks.values():
        v["industry_rank"] = ind.get(v["industry"], {}).get("rank") if v["industry"] else None

    # 舊版區塊（相容）
    try:
        from indicators_legacy import build as legacy_build
        legacy = legacy_build()
    except Exception as e:  # noqa: BLE001
        legacy = {"error": f"{type(e).__name__}: {e}"}
    ms = meta.get("markets") or {}
    out = {"generated_at": now_tw().isoformat(timespec="seconds"), "data_date": data_date,
           "price_basis": "還原價（adjlib：prices.csv + adj_factors.csv）",
           "market_status": {k: {"status": v.get("status"), "expected_latest": v.get("expected_latest"),
                                 "data_latest": v.get("data_latest")} for k, v in ms.items()
                             if isinstance(v, dict) and "status" in v},
           "quarter_end_window": quarter_end,
           "notes": ["外資買賣超可能包含 ETF 被動調整與避險部位，單日數字的意義有限，宜看多日累計與連續天數",
                     "籌碼各區塊有各自的 data_date（融資融券與外資持股常晚一個交易日公布）",
                     "集保股權分散只能從開始執行起每週累積；大戶相關規則在累積滿 config 週數前停用"],
           "params": {**(legacy.get("params") or {}),   # 舊欄位（atr_period、atr_mult、max_stop_pct、bench）：儀表板同步任務會讀
                      **{k: cfg[k] for k in ("indicators", "chips_indicators")}},
           "counts": {"stocks": len(stocks), "ok": sum(v["status"] == "ok" for v in stocks.values())},
           "stocks": strip_private(stocks), "industries": ind,
           "price_basis_legacy": legacy.get("price_basis"), "as_of_by_market": legacy.get("as_of_by_market"),
           "partial_bars_excluded": legacy.get("partial_bars_excluded"), "stale": legacy.get("stale"),
           "indicators": legacy.get("indicators", legacy)}
    write_json(OUT, out, compact=True)
    if len(bench_g):
        mk = market_block(bench_g, inst, mg, cfg, meta)
        mk["generated_at"] = out["generated_at"]
        mk["params"] = cfg["market"]
        write_json(MARKET_OUT, mk)
        log(f"market.json：{mk['data_date']} {mk['status']} {mk['status_reasons']}；階梯 {mk['ladder']['level']}")
    log(f"寫入 {OUT}：{len(stocks)} 檔（ok {out['counts']['ok']}）；產業 {len(ind)}；資料日期 {data_date}")


if __name__ == "__main__":
    main()
