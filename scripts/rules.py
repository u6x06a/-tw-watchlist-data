"""選股規則（階段 3）。evaluate(P, i) 只用「第 i 個交易日（含）以前」的資料，回傳當日訊號與旗標。
screen.py 用最新一天；paper.py / backtest.py 逐日呼叫同一個函式（不另寫一套）。參數在 config/rules.yaml screen 區塊。

opts（回測比較用的開關，預設全開）：
  use_chips_l1c   L1 止穩條件 c（外資賣超縮小 / 投信轉買）是否使用籌碼；關閉時改為「a、b 兩項都要」
  use_c1          C1 籌碼轉強（另受 config chips.tdcc_rules.c1_enabled 控制）
"""
import calendar
import warnings
from datetime import date

import numpy as np

RULE_ORDER = ["R1", "L1", "L2", "E1", "C1"]


def _f(x, n=4):
    if x is None:
        return None
    try:
        if np.isnan(x):
            return None
    except TypeError:
        return x
    return round(float(x), n)


def _row(df, i):
    return df.iloc[i] if df is not None else None


def _streak(mat, i, code, max_back=30):
    """第 i 日往回連續同號天數：+N 買超、−N 賣超；缺資料回傳 None。"""
    if mat is None:
        return None
    col = mat[code].values
    if np.isnan(col[i]):
        return None
    s = 0
    for k in range(i, max(-1, i - max_back), -1):
        v = col[k]
        if np.isnan(v) or v == 0:
            break
        if s == 0:
            s = 1 if v > 0 else -1
        elif (v > 0) == (s > 0):
            s += 1 if s > 0 else -1
        else:
            break
    return s


def revenue_asof(P, code, d):
    """截至日期 d 已公布的月營收（由舊到新）。opendata 有出表日期就用它；MOPS 彙總頁沒有，
    以法定期限（次月 10 日）當作公布日（保守估計，避免偷看未來）。"""
    cache = getattr(P, "_rev_cache", None)
    if cache is None:
        cache = {}
        rev = P.revenue
        if rev is not None and len(rev):
            def avail(r):
                y, m = int(r["ym"][:4]), int(r["ym"][5:7])
                y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
                sd = str(r.get("source_date") or "")
                return sd if str(r.get("source", "")).startswith("t187ap05") and sd else f"{y2:04d}-{m2:02d}-10"
            rev = rev.assign(avail=rev.apply(avail, axis=1)).sort_values(["code", "ym"])
            for c, g in rev.groupby("code"):
                cache[c] = g.reset_index(drop=True)
        P._rev_cache = cache
    g = cache.get(code)
    if g is None:
        return None
    g = g[g["avail"] <= d]
    return g if len(g) else None


def quarter_end_window(d, n):
    y, m = int(d[:4]), int(d[5:7])
    if m not in (3, 6, 9, 12):
        return False
    last = date(y, m, calendar.monthrange(y, m)[1])
    return int(np.busday_count(date.fromisoformat(d), last)) + 1 <= n


def trend_template(P, i, code):
    cfg = P.cfg["indicators"]["trend_template"]
    c = P.C[code].iat[i]
    m60, m150, m240 = P.ma(60)[code].iat[i], P.ma(150)[code].iat[i], P.ma(240)[code].iat[i]
    lb = int(cfg["ma240_rising_lookback"])
    m240p = P.ma(240)[code].iat[i - lb] if i >= lb else np.nan
    hi, lo = P.hh(250)[code].iat[i], P.ll(250)[code].iat[i]
    rs = P.rs_rank()[code].iat[i]
    vals = [m150, m240, m240p, m60, hi, lo, rs]
    if any(np.isnan(v) for v in vals):
        return None
    return bool(c > m150 > m240 and m240 > m240p and m60 > m150 and c > m60
                and c >= lo * (1 + float(cfg["min_above_52w_low_pct"]) / 100)
                and c >= hi * (1 - float(cfg["max_below_52w_high_pct"]) / 100)
                and rs >= float(cfg["min_rs_rank"]))


def liquid_mask(P, i):
    lq = P.cfg["screen"]["liquidity"]
    c = P.C.iloc[i]
    ok = c.notna() & P.sec_type.isin(lq["sec_types"]).reindex(c.index).values
    if lq["exclude_full_delivery"]:
        ok &= ~P.full_delivery.reindex(c.index).values
    ok &= (P.value20().iloc[i] >= float(lq["min_avg_value_20"])).values
    ok &= (P.bars().iloc[i] >= int(lq["min_bars"])).values
    return ok


def evaluate(P, i, opts=None):
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    opts = {"use_chips_l1c": True, "use_c1": True, **(opts or {})}
    cfg, sc = P.cfg, P.cfg["screen"]
    d = P.dates[i]
    b = P.bench
    C, O, H, L, V = P.C, P.O, P.H, P.L, P.V
    atr = P.atr(int(cfg["indicators"]["atr_period"]))
    ma = P.ma
    vma5, vma20 = P.vma(5), P.vma(20)
    ret = P.ret
    rs, irank = P.rs_rank(), P.industry_rank()
    mk = P.market_state()[i]
    filtered = bool(mk.get("filtered"))
    liquid = np.asarray(liquid_mask(P, i), dtype=bool)
    skip_unc = bool(cfg["adjust"].get("skip_adj_uncertain"))
    unc_n = int(sc["skip_adj_uncertain_days"])
    qend = quarter_end_window(d, int(cfg["indicators"]["quarter_end_window_days"]))
    signals, flags, skipped = [], [], []
    fn_ok = P.FN is not None and d in P.inst_dates
    mg_ok = P.MB is not None and d in P.margin_dates

    def base(code, rule):
        return {"code": code, "name": P.comp.loc[code, "name"] if code in P.comp.index else "",
                "industry": P.industry.get(code) or None, "industry_rank": _f(irank[code].iat[i], 0),
                "rs_rank": _f(rs[code].iat[i], 1), "rule": rule, "data_date": d, "market_filtered": filtered}

    def unc_recent(code):
        return skip_unc and bool(P.UNC[code].iloc[max(0, i - unc_n + 1): i + 1].any())

    def margin_chg(code, w):
        if P.MB is None or i < w:
            return None
        a, z = P.MB[code].iat[i - w], P.MB[code].iat[i]
        return None if np.isnan(a) or np.isnan(z) or a == 0 else (z / a - 1) * 100

    def chips_warning(code):
        r5 = ret(5)[code].iat[i]
        mc = margin_chg(code, 5)
        knife = None if mc is None or np.isnan(r5) else bool(r5 < 0 and mc >= float(
            cfg["chips_indicators"]["states"]["retail_catch_knife"]["min_margin_increase_pct"]))
        fs = _streak(P.FN, i, code) if fn_ok else None
        heavy = None if fs is None else fs <= -int(sc["r1"]["chips_warning_foreign_sell_streak"])
        if knife is None and heavy is None:
            return None, []
        why = (["散戶接刀（股價 5 日跌、融資 5 日增）"] if knife else []) + \
              ([f"外資連 {-fs} 日賣超"] if heavy else [])
        return bool(knife or heavy), why

    codes = [c for c, ok in zip(C.columns, liquid) if ok]
    if i < 25:
        return {"date": d, "signals": [], "flags": [], "skipped": [], "market": mk}

    # ---------------- R1
    r1 = sc["r1"]

    Cv, Ov, Vv = P.np("C"), P.np("O"), P.np("V")
    m20 = P.np_of(("ma", 20), ma(20))
    v5, v20 = P.np_of(("vma", 5), vma5), P.np_of(("vma", 20), vma20)
    with np.errstate(invalid="ignore", divide="ignore"):
        def trig(k):
            return ((Cv[k] / Cv[k - 1] - 1 > float(r1["min_gain_pct"]) / 100) & (Cv[k] > Ov[k])
                    & (Vv[k] > v5[k - 1] * float(r1["vol_mult"])) & (Cv[k] > m20[k]) & (m20[k] - m20[k - 5] >= 0))

        def squeeze(k):
            return v5[k] < v20[k] * float(r1["squeeze_ratio"])
        t0, s1 = trig(i), squeeze(i - 1)
        second = trig(i - 1) & squeeze(i - 2) & ~s1
    cand = liquid & t0 & (s1 | second)
    for j in np.where(cand)[0]:
        code = C.columns[j]
        nth = 1 if s1[j] else 2
        if unc_recent(code):
            skipped.append({"code": code, "rule": "R1", "reason": "adj_uncertain"})
            continue
        e, a = C[code].iat[i], atr[code].iat[i]
        stop = max(e - float(r1["stop_atr_mult"]) * a, e * (1 - float(r1["stop_max_pct"]) / 100))
        conf = None
        if P.FN is not None and all(P.dates[k] in P.inst_dates for k in range(i - int(r1["chips_confirm_days"]) + 1, i + 1)):
            w = slice(i - int(r1["chips_confirm_days"]) + 1, i + 1)
            conf = bool((P.FN[code].iloc[w] > 0).any() or (P.TN[code].iloc[w] > 0).any())
        warn, why = chips_warning(code)
        s = base(code, "R1")
        s.update({"nth_after_squeeze": nth, "trend_template_pass": trend_template(P, i, code),
                  "key": {"gain_pct": _f((e / C[code].iat[i - 1] - 1) * 100, 2),
                          "vol_vs_ma5_prev": _f(V[code].iat[i] / vma5[code].iat[i - 1], 2),
                          "vol_ma5_vs_ma20_prev": _f(vma5[code].iat[i - 1] / vma20[code].iat[i - 1], 3),
                          "ma20": _f(ma(20)[code].iat[i]), "atr14": _f(a)},
                  "entry_ref": _f(e), "stop": _f(stop), "risk_per_share": _f(e - stop),
                  "stop_basis": "entry-2ATR" if stop > e * (1 - float(r1["stop_max_pct"]) / 100) else "entry-10%",
                  "signal_bar_low": _f(L[code].iat[i]), "chips_confirm": conf, "chips_warning": warn,
                  "chips_warning_reasons": why, "needs_news_check": False})
        signals.append(s)

    # ---------------- L1
    l1 = sc["l1"]
    mcap = (P.RC.iloc[i] * P.shares.reindex(P.RC.columns)).where(P.sec_type.reindex(P.RC.columns) == "stock")
    top = set(mcap.dropna().sort_values(ascending=False).index[: int(l1["top_market_cap"])])
    br5 = ret(5)[b].iat[i]
    ind5 = P.industry_median(5)
    for code in [c for c in codes if c in top]:
        c0, a = C[code].iat[i], atr[code].iat[i]
        hh20 = H[code].iloc[i - 19: i + 1].max()
        deep = bool(c0 <= hh20 - float(l1["deep_atr_mult"]) * a or c0 < ma(20)[code].iat[i] * float(l1["deep_ma20_ratio"]))
        if not deep:
            continue
        rv = revenue_asof(P, code, d)
        if rv is not None and len(rv) >= 3:
            q = float(rv["yoy_pct"].astype(float).tail(3).mean())
            quality = "ok" if q > float(l1["quality_min_yoy_3m"]) else "fail"
        else:
            q, quality = None, "unknown"
        if quality == "fail":
            continue
        r5 = ret(5)[code].iat[i]
        im = ind5[code].iat[i]
        systemic = bool((not np.isnan(br5) and r5 >= br5 * float(l1["systemic_mult"])) or (not np.isnan(im) and im < 0))
        o0, h1, l0 = O[code].iat[i], H[code].iat[i - 1], L[code].iat[i]
        body = abs(c0 - o0)
        lower = min(c0, o0) - l0
        cond_a = bool(lower >= body * float(l1["lower_shadow_body_mult"]) and lower > 0) or bool(c0 > h1)
        cond_b = bool(V[code].iat[i] < V[code].iloc[i - 3: i].mean())
        cond_c = None
        if opts["use_chips_l1c"] and P.FN is not None and all(P.dates[k] in P.inst_dates for k in range(i - 3, i + 1)):
            fv = P.FN[code].iloc[i - 3: i + 1].values * P.RC[code].iloc[i - 3: i + 1].values   # 張 × 收盤 ≈ 金額（千元）
            prev = np.nanmean(fv[:3])
            shrink = bool(prev < 0 and fv[3] >= prev * (1 - float(l1["foreign_sell_shrink_pct"]) / 100))
            t = P.TN[code].iloc[i - 1: i + 1].values
            trust_turn = bool(t[1] > 0 and t[0] <= 0)
            cond_c = bool(shrink or trust_turn)
        conds = [cond_a, cond_b] + ([cond_c] if cond_c is not None else [])
        stab = sum(conds) >= 2 if cond_c is not None else (cond_a and cond_b)
        if unc_recent(code):
            skipped.append({"code": code, "rule": "L1", "reason": "adj_uncertain"})
            continue
        k_hi = i - 19 + int(np.nanargmax(H[code].iloc[i - 19: i + 1].values))
        lb = int(l1["invalidation_lookback"])
        inval = L[code].iloc[max(0, k_hi - lb): k_hi].min()
        mc5 = margin_chg(code, 5)
        warn = None if mc5 is None else bool(r5 < 0 and mc5 > 0)
        s = base(code, "L1")
        s.update({"status": "可分批第一批" if stab else "跌深觀察", "quality": quality,
                  "idiosyncratic": not systemic,
                  "key": {"close": _f(c0), "high_20d": _f(hh20), "atr14": _f(a), "ma20": _f(ma(20)[code].iat[i]),
                          "ret5_pct": _f(r5 * 100, 2), "bench_ret5_pct": _f(br5 * 100, 2),
                          "industry_ret5_median_pct": _f(im * 100, 2), "revenue_yoy_3m_avg": _f(q, 2)},
                  "stabilize": {"a_shadow_or_above_prev_high": cond_a, "b_vol_below_3d_avg": cond_b,
                                "c_foreign_shrink_or_trust_turn": cond_c,
                                "rule": "三項中兩項" if cond_c is not None else "籌碼資料缺或關閉：a、b 都要"},
                  "plan": {"batch1": _f(c0) if stab else None, "batch2_lower_1atr": _f(c0 - a),
                           "batch2_reclaim_ma5": _f(ma(5)[code].iat[i]), "batch3_reclaim_ma20": _f(ma(20)[code].iat[i])},
                  "entry_ref": _f(c0), "stop": _f(inval), "risk_per_share": _f(c0 - inval),
                  "invalidation": {"price": _f(inval), "basis": f"下跌起點 {P.dates[k_hi]} 前 {lb} 日最低點",
                                   "cost_stop_pct": float(l1["cost_stop_pct"]), "cost_stop_note": "平均成本 −15% 由儀表板端計算"},
                  "chips_warning": warn, "chips_warning_reasons": ["下跌期間融資增加（散戶接刀，止穩可信度下降）"] if warn else [],
                  "needs_news_check": True})
        signals.append(s)

    # ---------------- L2
    l2 = sc["l2"]
    for w in l2["windows"]:
        medv = P.industry_median(int(w)).values[i]
        rv_ = ret(int(w)).values[i]
        with np.errstate(invalid="ignore"):
            hit = liquid & (medv <= float(l2["industry_drop_pct"]) / 100) & (rv_ <= medv)
        for j in np.where(hit)[0]:
            code = C.columns[j]
            m, r = medv[j], rv_[j]
            if any(s["code"] == code and s["rule"] == "L2" for s in signals):
                continue
            rv = revenue_asof(P, code, d)
            yoy = float(rv["yoy_pct"].iloc[-1]) if rv is not None else None
            s = base(code, "L2")
            s.update({"key": {"window_days": int(w), "ret_pct": _f(r * 100, 2), "industry_median_pct": _f(m * 100, 2),
                              "revenue_yoy_pct": _f(yoy, 2)},
                      "revenue_positive": None if yoy is None else yoy > 0, "candidate_only": True,
                      "entry_ref": _f(C[code].iat[i]), "stop": None, "risk_per_share": None, "needs_news_check": True})
            signals.append(s)

    # ---------------- E1
    e1 = sc["e1"]
    if mk.get("ladder_new"):
        w = int(e1["decay_window"])
        dec = None
        if "00685L" in C.columns and i >= w:
            dec = (ret(w)["00685L"].iat[i] - 2 * ret(w)[b].iat[i]) * 100
        vol = (C[b].pct_change().iloc[i - w + 1: i + 1].std() * 100) if i >= w else None
        for code in e1["codes"]:
            if code not in C.columns or np.isnan(C[code].iat[i]):
                continue
            s = base(code, "E1")
            s.update({"key": {"ladder_level": mk["ladder_level"], "new_steps": mk["ladder_new"],
                              "cycle_start": mk["ladder_start"], "bench_change_pct": _f(mk["change_pct"], 2)},
                      "ratio": e1["ratio"], "entry_ref": _f(C[code].iat[i]), "stop": None, "risk_per_share": None,
                      "needs_news_check": False})
            if code == "00685L":
                s["decay_estimate"] = {f"ret_{w}d_minus_2x_0050_pct": _f(dec, 2),
                                       f"0050_daily_vol_{w}d_pct": _f(vol, 2),
                                       "note": "以 0050 近似加權指數；正二追蹤加權指數，數字僅供參考"}
            signals.append(s)

    # ---------------- C1（集保未累積足夠週數前停用）
    tdr = cfg["chips"]["tdcc_rules"]
    c1_note = None
    if not (tdr.get("c1_enabled") and opts["use_c1"]):
        c1_note = f"C1 停用：集保資料需累積 {tdr['min_weeks']} 週（config chips.tdcc_rules.c1_enabled）"
    elif P.tdcc is not None:
        c1 = sc["c1"]
        tdates = [x for x in P.tdcc.index if x <= d]
        for code in codes:
            m60 = ma(60)[code].iloc[i - int(c1["min_days"]) + 1: i + 1].values
            cc = C[code].iloc[i - int(c1["min_days"]) + 1: i + 1].values
            if np.isnan(m60).any() or (np.abs(cc / m60 - 1) * 100 > float(c1["ma60_band_pct"])).any():
                continue
            if code not in P.tdcc.columns or len(tdates) < 3:
                continue
            p = P.tdcc.loc[tdates[-3:], code].values
            tw = int(c1["trust_buy_window"])
            t5 = P.TN[code].iloc[i - tw + 1: i + 1].sum() if P.TN is not None else np.nan
            if not (p[2] > p[1] > p[0] and t5 > 0 and cc[-1] < np.nanmax(H[code].iloc[i - int(c1["min_days"]) + 1: i].values)):
                continue
            s = base(code, "C1")
            s.update({"key": {"pct_1000plus_3w": [_f(x, 3) for x in p], "trust_net_5d_lots": _f(t5, 1)},
                      "observe_only": True, "entry_ref": _f(cc[-1]), "stop": None, "risk_per_share": None,
                      "needs_news_check": False})
            signals.append(s)

    # ---------------- 3-5 旗標（全市場流動股）：先用矩陣一次算完全市場，再組成每檔的旗標清單
    fl = sc["flags"]
    Hv, Lv = P.np("H"), P.np("L")
    wv = int(fl["weak_vs_market_window"])
    with np.errstate(invalid="ignore", divide="ignore"):
        c0, c1_, c2 = Cv[i], Cv[i - 1], Cv[i - 2]
        body = np.abs(c0 - Ov[i])
        upper = Hv[i] - np.maximum(c0, Ov[i])
        F = {}
        F["limit_up_then_heavy_black"] = ((c1_ / c2 - 1) * 100 >= float(fl["limit_up_pct"])) &             (Vv[i] >= Vv[i - 1] * float(fl["heavy_vol_mult"])) & ((c0 < Ov[i]) | (upper >= body * float(fl["upper_shadow_body_mult"])))
        lost = np.ones(len(c0), bool)
        for n in (10, 20, 60):
            mv = P.np_of(("ma", n), ma(n))
            F[f"break_ma{n}"] = (c0 < mv[i]) & (c1_ >= mv[i - 1])
            lost &= c0 < mv[i]
        F["all_ma_lost"] = lost & (c0 < P.np_of(("ma", 5), ma(5))[i])
        hi = P.np_of(("hh", 250, "H"), P.hh(250))[i]
        near_hi = c0 >= hi * (1 - float(fl["near_high_pct"]) / 100)
        bret = ret(wv)[b].iat[i]
        F["weak_vs_market"] = (bret > 0) & (ret(wv).values[i] < 0) & (v5[i] < v20[i])
        if fn_ok:
            FNv, TNv = P.FN.values, P.TN.values
            k = int(fl["opposite_streak"])
            fsell = (FNv[i - k + 1: i + 1] < 0).all(axis=0)
            tbuy = (TNv[i - k + 1: i + 1] > 0).all(axis=0)
            F["foreign_trust_opposite_at_high"] = near_hi & fsell & tbuy
            sd, bd = int(fl["trust_abandon_sell_days"]), int(fl["trust_abandon_prior_buy_days"])
            F["trust_abandon"] = (TNv[i - sd + 1: i + 1] < 0).all(axis=0) & (TNv[i - sd - bd + 1: i - sd + 1] > 0).all(axis=0)
        if P.FP is not None and i >= 20:
            fpd = P.FP.values[i] - P.FP.values[i - 20]
            F["foreign_holding_drop"] = fpd <= -float(fl["foreign_pct_drop_pp"])
        else:
            fpd = None
        if mg_ok and fn_ok and i >= 5:
            MBv = P.MB.values
            mch = (MBv[i] / MBv[i - 5] - 1) * 100
            inst5 = np.nansum(P.FN.values[i - 4: i + 1], axis=0) + np.nansum(P.TN.values[i - 4: i + 1], axis=0)
            F["chips_weakening_at_high"] = (c0 >= np.nanmax(Cv[i - 19: i + 1], axis=0)) &                 (mch >= float(fl["margin_surge_pct"])) & (inst5 < 0)
    names = list(F)
    M = np.vstack([np.nan_to_num(F[n].astype(float)) > 0 for n in names]) & liquid
    # 利多可能已反映（需營收資料，只對有營收的流動股逐檔檢查）
    good = {}
    nd = int(fl["good_news_days"])
    for j in np.where(liquid)[0]:
        code = C.columns[j]
        rv = revenue_asof(P, code, d)
        if rv is None:
            continue
        last = rv.iloc[-1]
        revs = rv["revenue_k"].astype(float).values
        hi12 = len(revs) >= 12 and revs[-1] >= np.nanmax(revs[-12:])
        if not (hi12 or float(last["yoy_pct"]) >= float(fl["good_news_yoy_pct"])):
            continue
        k0 = int(np.searchsorted(P.dates, last["avail"]))
        if k0 + nd <= i and i - 5 <= k0 + nd and Cv[k0 + nd, j] / Cv[k0, j] - 1 <= 0:
            good[j] = last["ym"]
    for j in sorted(set(np.where(M.any(axis=0))[0]) | set(good)):
        code = C.columns[j]
        out = [n for k_, n in enumerate(names) if M[k_, j]]
        extra = {}
        if "foreign_trust_opposite_at_high" in out and qend:
            extra["window_dressing_possible"] = True
        if "foreign_holding_drop" in out and fpd is not None:
            extra["foreign_pct_chg_20d"] = _f(fpd[j], 3)
        if j in good:
            out.append("good_news_priced_in")
            extra["revenue_ym"] = good[j]
        f = base(code, "FLAG")
        f.pop("rule")
        f.update({"flags": out, **extra})
        flags.append(f)

    # 排序：規則分組，組內 R1 先趨勢模板通過者，再依 RS 排名（高→低）、產業排名（強→弱）
    def key(s):
        return (RULE_ORDER.index(s["rule"]), 0 if s.get("trend_template_pass") else 1,
                -(s.get("rs_rank") or -1), s.get("industry_rank") or 999)
    signals.sort(key=key)
    return {"date": d, "signals": signals, "flags": flags, "skipped": skipped, "market": mk,
            "market_wide_selloff": bool(mk.get("change_pct") is not None and mk["change_pct"] <= float(fl["market_selloff_pct"])),
            "quarter_end_window": qend, "c1_note": c1_note,
            "chips_data": {"institutional": fn_ok, "margin": mg_ok}}

