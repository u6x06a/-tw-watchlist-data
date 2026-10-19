#!/usr/bin/env python3
"""由 data/prices.csv（原始、未還原，不修改）產生全市場還原價與跳動檢查清單。

輸出：
  data/prices_adj.csv     欄位同 prices.csv（code,date,open,high,low,close,volume）。台股為還原價；GLOBAL（美股/韓股/匯率）
                          照 yfinance 原值（yfinance 已調整分割、未調整股利）。範圍由 config adjust.output_scope 決定。
                          config adjust.bad_rows 列出的 K 棒不輸出。後續指標與回測一律讀這個檔。
  data/adj_factors.csv    每一個還原事件：code,date,prev_close,ref_price,factor,volume_factor,reason,source
                          （factor = 參考價 / 前一日收盤；事件日以前的價格乘上 factor 的累積乘積）
  data/jump_review.csv    原始資料單日漲跌超過 config adjust.default_limit_pct.stock 的紀錄與判定原因（供人工確認）
  data/adjustments.json   摘要：事件來源狀態、各原因筆數、還原後仍超過漲跌幅限制（未解決）的清單、資料日期
  data/events_auto.json   交易所除權息事件快取（全市場）
還原事件來源（優先順序）：
  1. 交易所除權息表：證交所 TWT49U、櫃買 exDailyQ 的「除權息前收盤價 / 除權息參考價」（每次重抓，失敗用快取）
  2. config adjust.manual_events（手動登錄，需附來源）
  3. data/ref_events.csv：全市場日行情「漲跌」欄反推的參考價 ≠ 前收（分割、減資、面額變更等，scripts/refprice.py）
     參考價 < 前收且變動超過 config adjust.split_like_min_change → 推定為分割/面額變更；> 前收 → 推定為減資；
     變動較小 → 其他參考價調整。成交量只在推定分割/減資時反向調整（除權息不調整成交量）。

用法：python scripts/adjust.py [--no-fetch] [--report]
  --no-fetch  不連網，只用快取的除權息事件
  --report    另外寫 reports/jumps_review_YYYYMMDD.md（給人看的跳動檢查報告）
"""
import json
import os
import sys

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adjlib import apply_factors  # noqa: E402
from common import (CODE_OK, get, load_config, log, now_tw, path, read_json, roc_to_iso,  # noqa: E402
                    sec_type_from_code, write_json)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SRC = path("data", "prices.csv")
DST = path("data", "prices_adj.csv")
FACTORS = path("data", "adj_factors.csv")
REVIEW = path("data", "jump_review.csv")
LOG = path("data", "adjustments.json")
CACHE = path("data", "events_auto.json")
REF_EVENTS = path("data", "ref_events.csv")
REF_COV = path("data", "ref_events_coverage.json")


def _manual_splits():
    """{code: [{effective, ratio, source}]}，供 indicators.py 的 lot_price_factor 使用（相容舊介面）。"""
    try:
        out = {}
        for m in load_config()["adjust"].get("manual_events") or []:
            if m.get("type") == "split":
                out.setdefault(m["code"], []).append({"effective": m["date"], "ratio": m["ratio"], "source": m["source"]})
        return out
    except Exception:  # noqa: BLE001
        return {}


SPLITS = _manual_splits()


def _f(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def fetch_events(start, end, sleep, exright_ref="dividend_only"):
    """交易所除權息、減資、變更面額事件（全市場）：[{code,date,prev_close,ref,factor,kind,market,category,source}]。
    除權息的參考價（config adjust.exright_ref）：
      dividend_only  用「減除股利參考價」（只扣股票股利與現金股利；交易所的漲跌停與開盤基準即以此計算）
      full           用「除權息參考價」（另含現金增資認購權的價值）
    兩者只在有現金增資認購時不同（實測天瀚 2026-08-18：前收 44.40、除權息參考價 30.04、減除股利參考價 44.40、
    當日漲停 48.80 = 44.40 × 1.1）。"""
    s = requests.Session()
    out = []
    ref_col = "減除股利參考價" if exright_ref == "dividend_only" else "除權息參考價"
    j = get(s, "https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
            {"startDate": start.replace("-", ""), "endDate": end.replace("-", ""), "response": "json"}, sleep=sleep).json()
    if j.get("stat") != "OK":
        raise ValueError(f"TWT49U: {j.get('stat')}")
    f = j["fields"]
    ic, ip, ir, ik = f.index("股票代號"), f.index("除權息前收盤價"), f.index(ref_col), f.index("權/息")
    ifull = f.index("除權息參考價")
    for d in j.get("data", []):
        out.append(("TWSE", roc_to_iso(d[0]), d[ic].strip(), _f(d[ip]), _f(d[ir]) or _f(d[ifull]), d[ik].strip(),
                    _f(d[ifull])))
    j = get(s, "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
            {"startDate": start.replace("-", "/"), "endDate": end.replace("-", "/"), "response": "json"}, sleep=sleep).json()
    for t in j.get("tables", []):
        f = [x.strip() for x in t.get("fields", [])]
        if "代號" not in f:
            continue
        ic, ip, ir, ik = f.index("代號"), f.index("除權息前收盤價"), f.index(ref_col), f.index("權/息")
        ifull = f.index("除權息參考價")
        for d in t.get("data", []):
            out.append(("TPEX", roc_to_iso(d[0]), d[ic].strip(), _f(d[ip]), _f(d[ir]) or _f(d[ifull]), d[ik].strip(),
                        _f(d[ifull])))
    events = [{"code": c, "date": dt, "prev_close": p, "ref": r, "factor": r / p, "kind": k, "market": m,
               "category": "exright", "source": "TWT49U" if m == "TWSE" else "exDailyQ", "ref_full": rf,
               "ref_basis": exright_ref}
              for m, dt, c, p, r, k, rf in out if CODE_OK.match(c) and p and r]

    # 減資、變更面額：恢復買賣日的參考價 / 停止買賣前收盤價（交易所公布）
    def table_events(url, params, market, category, source, f_date, f_code, f_prev, f_ref, f_kind=None):
        j = get(s, url, params, sleep=sleep).json()
        tables = j.get("tables") or [j]
        for t in tables:
            f = [x.strip() for x in t.get("fields", [])]
            if f_code not in f:
                continue
            for d in t.get("data", []):
                c, p, r = str(d[f.index(f_code)]).strip(), _f(d[f.index(f_prev)]), _f(d[f.index(f_ref)])
                if CODE_OK.match(c) and p and r:
                    events.append({"code": c, "date": roc_to_iso(d[f.index(f_date)]), "prev_close": p, "ref": r,
                                   "factor": r / p, "kind": str(d[f.index(f_kind)]).strip() if f_kind else "",
                                   "market": market, "category": category, "source": source})

    s1, e1 = start.replace("-", ""), end.replace("-", "")
    s2, e2 = start.replace("-", "/"), end.replace("-", "/")
    table_events("https://www.twse.com.tw/rwd/zh/reducation/TWTAUU",
                 {"startDate": s1, "endDate": e1, "response": "json"}, "TWSE", "reduction", "TWTAUU",
                 "恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "減資原因")
    table_events("https://www.twse.com.tw/rwd/zh/change/TWTB8U",
                 {"startDate": s1, "endDate": e1, "response": "json"}, "TWSE", "parvalue", "TWTB8U",
                 "恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價")
    table_events("https://www.tpex.org.tw/www/zh-tw/bulletin/revivt",
                 {"startDate": s2, "endDate": e2, "response": "json"}, "TPEX", "reduction", "revivt",
                 "恢復買賣日期", "股票代號", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格", "減資原因")
    return sorted(events, key=lambda x: (x["code"], x["date"]))


def load_events(first, last, no_fetch, sleep, exright_ref="dividend_only"):
    status, events = "cache", None
    if not no_fetch:
        try:
            events = fetch_events(first, last, sleep, exright_ref)
            write_json(CACHE, events)
            status = "fetched"
        except Exception as e:  # noqa: BLE001
            log(f"除權息事件抓取失敗，改用快取：{type(e).__name__}: {e}")
            status = f"fetch failed ({type(e).__name__}), cache"
    if events is None:
        events = read_json(CACHE, []) or []
        if not events:
            status += "（沒有快取，除權息未還原）"
    return events, status


def lot_price_factor(code, date, events, split_adjusted=False):
    """把「date 當天成交」的價格換算到今天的還原價基準（indicators.py 的本機停損計算使用）。"""
    f = 1.0
    for ev in events:
        if ev["code"] == code and date < ev["date"]:
            f *= ev["factor"]
    if not split_adjusted:
        for sp in SPLITS.get(code, []):
            if date < sp["effective"]:
                f /= sp["ratio"]
    return f


def build_event_table(events_ex, ref_df, manual, a, date_idx=None):
    """(code, date) -> 事件。回傳 (table, unresolved_marks, skipped)。
    櫃買在「無成交日」之後的漲跌，基準不是最後一筆成交價（實測 499 筆全部發生在 gap ≥ 2 的日子），
    這類變動未達 split_like_min_change 的參考價差異不是公司事件，不套用（記在 skipped）。"""
    big = float(a["split_like_min_change"])
    date_idx = date_idx or {}
    table, marks, skipped = {}, [], []
    conflicts = []
    for ev in events_ex:
        cat = ev.get("category", "exright")
        k = (ev["code"], ev["date"])
        if cat == "exright":
            reason, vf = f"除權息（{ev['kind']}）", 1.0
            rf = ev.get("ref_full")
            if rf and abs(rf - ev["ref"]) > 0.005:
                reason += f"，含現金增資認購（除權息參考價 {rf:g}；依 exright_ref={ev.get('ref_basis')} 還原）"
        elif cat == "reduction":
            reason, vf = f"減資（{ev['kind']}）", 1 / ev["factor"]
        else:
            reason, vf = "變更面額", 1 / ev["factor"]
        if k in table:   # 同一天兩種事件：保留減資/面額表（其參考價已以停止買賣前收盤計算），記錄衝突
            conflicts.append({"code": k[0], "date": k[1], "kept": table[k]["reason"] if cat == "exright" else reason})
            if cat == "exright":
                continue
        table[k] = {"code": ev["code"], "date": ev["date"], "prev_close": ev["prev_close"], "ref_price": ev["ref"],
                    "factor": ev["factor"], "volume_factor": vf, "reason": reason,
                    "source": ev.get("source") or ("TWT49U" if ev["market"] == "TWSE" else "exDailyQ")}
    if conflicts:
        log(f"同日多種事件：{conflicts}")
    for m in manual or []:
        k = (m["code"], m["date"])
        if k in table:
            continue
        if m["type"] == "split":
            r = float(m["ratio"])
            table[k] = {"code": m["code"], "date": m["date"], "prev_close": None, "ref_price": None,
                        "factor": 1 / r, "volume_factor": r, "reason": f"分割 1 拆 {m['ratio']:g}（手動登錄）",
                        "source": f"config: {m['source']}"}
        else:
            raise ValueError(f"manual_events 不支援的 type：{m['type']}")
    ex_dates = {}
    for k in table:
        ex_dates.setdefault(k[0], []).append(k[1])
    if ref_df is not None:
        for r in ref_df.itertuples(index=False):
            k = (r.code, r.date)
            if k in table:
                continue
            # 前一筆資料日之後、當天（含）以前已有事件（例如除權息日落在休市日）→ 參考價差異已被解釋，不重複套用
            if any((r.prev_date or "") < d <= r.date for d in ex_dates.get(r.code, [])):
                continue
            ref, prev = _f(r.ref_price), _f(r.prev_close)
            if ref and prev:
                f = ref / prev
                gap = date_idx.get(r.date, 0) - date_idx.get(r.prev_date, 0) if r.prev_date in date_idx else 0
                if r.market == "TPEX" and gap > 1 and abs(f - 1) < big:
                    skipped.append({"code": r.code, "date": r.date, "prev_date": r.prev_date, "pct": round((f - 1) * 100, 2)})
                    continue
                if f < 1 - big:
                    reason, vf = "分割或面額變更（推定：參考價低於前收）", 1 / f
                elif f > 1 + big:
                    reason, vf = "減資（推定：參考價高於前收）", 1 / f
                else:
                    reason, vf = "其他參考價調整（除權息表未列）", 1.0
                table[k] = {"code": r.code, "date": r.date, "prev_close": prev, "ref_price": ref, "factor": f,
                            "volume_factor": vf, "reason": reason, "source": f"ref_events({r.market} 漲跌欄)"}
            elif str(r.mark or "").strip():
                marks.append({"code": r.code, "date": r.date, "mark": r.mark})
    return table, marks, skipped


def main():
    cfg = load_config()
    a = cfg["adjust"]
    sleep = float(cfg["data"]["request_sleep_sec"])
    df = pd.read_csv(SRC, dtype=str, keep_default_na=False)
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    is_tw = df["code"].map(lambda c: bool(CODE_OK.match(c))).values
    tw_dates = sorted(df.loc[is_tw, "date"].unique())
    data_date = tw_dates[-1]
    events_ex, ev_status = load_events(tw_dates[0], data_date, "--no-fetch" in sys.argv, sleep, a["exright_ref"])
    ref_df = pd.read_csv(REF_EVENTS, dtype=str, keep_default_na=False) if os.path.exists(REF_EVENTS) else None
    cov = {m: set(v) for m, v in (read_json(REF_COV, {}) or {}).items()}
    date_idx = {d: i for i, d in enumerate(tw_dates)}
    table, marks, skipped = build_event_table(events_ex, ref_df, a.get("manual_events"), a, date_idx)
    mark_keys = {(m["code"], m["date"]) for m in marks}

    # ---- 套用還原係數（與 adjlib.load_adjusted 同一套）----
    close = pd.to_numeric(df["close"], errors="coerce").values
    pf, vf = apply_factors(df, table.values())
    by_code = {}
    for ev in table.values():
        by_code.setdefault(ev["code"], []).append(ev)
    adj = df.copy()
    chg = (pf != 1.0) & is_tw
    for col in ("open", "high", "low", "close"):
        v = pd.to_numeric(adj.loc[chg, col], errors="coerce") * pf[chg]
        adj.loc[chg, col] = v.round(4).map(lambda x: "" if pd.isna(x) else f"{x:.4f}".rstrip("0").rstrip("."))
    vchg = (vf != 1.0) & is_tw
    v = pd.to_numeric(adj.loc[vchg, "volume"], errors="coerce") * vf[vchg]
    adj.loc[vchg, "volume"] = v.round(0).map(lambda x: "" if pd.isna(x) else f"{x:.0f}")
    bad = {(b["code"], b["date"]) for b in (a.get("bad_rows") or [])}
    keep = ~pd.Series(list(zip(adj["code"], adj["date"]))).isin(bad).values
    if a["output_scope"] == "tracked":
        from fetch_prices import GLOBAL, WATCHLIST
        keep &= adj["code"].isin(set(WATCHLIST) | set(GLOBAL)).values
    adj[keep].to_csv(DST + ".tmp", index=False, lineterminator="\r\n")   # 與 prices.csv 相同的 CRLF
    os.replace(DST + ".tmp", DST)

    # ---- 事件前收盤 vs CSV 前一交易日收盤（不一致代表資料缺口或事件來源有誤）----
    prev_close_csv = pd.Series(close).groupby(df["code"]).shift().values
    pos = {(c, d): i for i, (c, d) in enumerate(zip(df["code"], df["date"]))}
    mismatches = []
    for ev in table.values():
        i = pos.get((ev["code"], ev["date"]))
        if i is None or ev["prev_close"] is None or np.isnan(prev_close_csv[i]):
            continue
        if abs(prev_close_csv[i] - ev["prev_close"]) > 0.011:
            mismatches.append({"code": ev["code"], "date": ev["date"], "event_prev_close": ev["prev_close"],
                               "csv_prev_close": float(prev_close_csv[i]), "source": ev["source"]})

    # ---- 跳動檢查 ----
    comp = {}
    if os.path.exists(path("data", "companies.csv")):
        c = pd.read_csv(path("data", "companies.csv"), dtype=str, keep_default_na=False)
        comp = {r.code: r for r in c.itertuples(index=False)}
    date_idx = {d: i for i, d in enumerate(tw_dates)}
    adj_close = close * pf
    prev_adj = pd.Series(adj_close).groupby(df["code"]).shift().values
    prev_date = df.groupby("code")["date"].shift().values
    ret = close / prev_close_csv - 1
    adj_ret = adj_close / prev_adj - 1
    base = float(a["default_limit_pct"]["stock"])
    tol = float(a["jump_tolerance_pct"])
    ipo_days = int(a["ipo_no_limit_days"])
    rows, unresolved = [], []

    def limit_of(code):
        r = comp.get(code)
        lim = r.limit_pct if r is not None else ""
        if lim == "none":
            return None
        if lim:
            return float(lim)
        st = r.sec_type if r is not None else sec_type_from_code(code)
        return float(a["default_limit_pct"]["stock" if st in ("stock", "preferred") else "etf"])

    def mkt_of(code):
        r = comp.get(code)
        return r.market if r is not None else ""

    def is_ipo(code, d):
        r = comp.get(code)
        ld = getattr(r, "listed_date", "") if r is not None else ""
        if not ld or ld > d:
            return False
        return sum(1 for x in tw_dates if ld <= x <= d) <= ipo_days

    for i in np.where(is_tw & ~np.isnan(ret))[0]:
        code, d = df.at[i, "code"], df.at[i, "date"]
        lim = limit_of(code)
        r_pct, a_pct = ret[i] * 100, adj_ret[i] * 100
        over_raw = abs(r_pct) > base + tol
        over_adj = lim is not None and abs(a_pct) > lim + tol and not is_ipo(code, d)
        if not over_raw and not over_adj:
            continue
        # 事件日 = 這一根，或落在前一筆資料日與這一根之間（例如除權息日剛好是休市日或停牌期間）
        evs = [e for e in by_code.get(code, []) if (prev_date[i] or "") < e["date"] <= d]
        ev = None
        if evs:
            ev = dict(evs[-1])
            if len(evs) > 1:
                ev["reason"] = "＋".join(e["reason"] for e in evs)
                ev["source"] = "＋".join(e["source"] for e in evs)
                ev["factor"] = float(np.prod([e["factor"] for e in evs]))
        gap = date_idx.get(d, 0) - date_idx.get(prev_date[i], 0)
        skipped_here = any(s["code"] == code and s["date"] == d for s in skipped) if mkt_of(code) == "TPEX" else False
        mkt = comp[code].market if code in comp else ""
        covered = d in cov.get(mkt, set()) if mkt else any(d in v for v in cov.values())
        if ev:
            cat, adjusted, confirm = ev["reason"], "Y", "N"
        elif (code, d) in mark_keys:
            cat, adjusted, confirm = "交易所標示除權息，但除權息表查無參考價", "N", "Y"
        elif is_ipo(code, d):
            cat, adjusted, confirm = f"新上市首 {ipo_days} 日無漲跌幅限制", "N", "N"
        elif lim is None:
            cat, adjusted, confirm = "無漲跌幅限制（國外成分 ETF 等）", "N", "N"
        elif abs(r_pct) <= lim + tol:
            cat, adjusted, confirm = f"在該檔漲跌幅限制 {lim:g}% 內（槓桿/反向 ETF）", "N", "N"
        elif gap > 1 and skipped_here:
            cat, adjusted, confirm = "櫃買無成交日後恢復成交（多日累積漲跌，非公司事件）", "N", "N"
        elif not covered:
            cat, adjusted, confirm = "原因不明：該日尚未用漲跌欄檢查參考價", "N", "Y"
        else:
            cat, adjusted, confirm = "原因不明：參考價 = 前收卻超過漲跌幅（疑似壞資料）", "N", "Y"
        uncertain = "N"
        if over_adj and adjusted == "Y":
            confirm = "Y"
            cat += "；還原後仍超過漲跌幅限制"
            # 還原基準有疑義（例如現金增資除權：交易所漲停用前收、跌停用含認購權參考價），標 adj_uncertain
            uncertain = "Y"
            for e in evs:
                table[(e["code"], e["date"])]["adj_uncertain"] = "Y"
        row = {"code": code, "name": comp[code].name if code in comp else "", "market": mkt,
               "sec_type": comp[code].sec_type if code in comp else sec_type_from_code(code),
               "limit_pct": "none" if lim is None else f"{lim:g}", "date": d, "prev_date": prev_date[i],
               "gap_trading_days": gap, "prev_close": prev_close_csv[i], "close": close[i],
               "ret_pct": round(r_pct, 2), "adj_ret_pct": round(a_pct, 2), "category": cat,
               "adjusted": adjusted, "needs_confirm": confirm,
               "event_source": ev["source"] if ev else "", "factor": round(ev["factor"], 6) if ev else "",
               "adj_uncertain": uncertain,
               "note": f"停牌/無成交 {gap - 1} 個交易日後恢復" if gap > 1 else ""}
        rows.append(row)
        if confirm == "Y":
            unresolved.append({k: row[k] for k in ("code", "name", "date", "ret_pct", "adj_ret_pct", "category")})
    rev = pd.DataFrame(rows)
    rev.to_csv(REVIEW, index=False)
    for e in table.values():
        e.setdefault("adj_uncertain", "N")
    fac = pd.DataFrame(sorted(table.values(), key=lambda e: (e["code"], e["date"])))
    fac = fac[fac["date"] >= tw_dates[0]] if len(fac) else fac
    fac.to_csv(FACTORS, index=False, float_format="%.8g")
    by_cat = rev["category"].value_counts().to_dict() if len(rev) else {}
    write_json(LOG, {
        "generated_at": now_tw().isoformat(timespec="seconds"), "data_date": data_date,
        "price_basis": "台股：還原分割/減資/除權息（往前乘係數）；GLOBAL：yfinance 原值",
        "events_status": ev_status, "events_by_reason": fac["reason"].value_counts().to_dict() if len(fac) else {},
        "ref_events_coverage": {m: [min(v), max(v), len(v)] for m, v in cov.items() if v},
        "manual_events": a.get("manual_events") or [], "bad_rows_excluded": sorted(bad),
        "event_prev_close_mismatch": mismatches,
        "tpex_no_trade_ref_diff_skipped": {"count": len(skipped), "note": "櫃買無成交日後的漲跌基準差異，非公司事件，未還原",
                                           "examples": skipped[:20]},
        "jump_review": {"rows": len(rev), "by_category": by_cat, "needs_confirm": unresolved},
        "output_scope": a["output_scope"], "rows": int(keep.sum())})
    log(f"寫入 {DST}（{int(keep.sum())} 筆）；事件 {len(table)}（{ev_status}）；跳動 {len(rev)} 筆，待確認 {len(unresolved)}；"
        f"前收不符 {len(mismatches)}")
    if "--report" in sys.argv:
        write_report(rev, data_date, ev_status, mismatches)


def write_report(rev, data_date, ev_status, mismatches):
    os.makedirs(path("reports"), exist_ok=True)
    out = path("reports", f"jumps_review_{now_tw():%Y%m%d}.md")
    lines = [f"# 單日跳動檢查（資料日期 {data_date}）", "",
             f"產生時間：{now_tw():%Y-%m-%d %H:%M}；除權息事件來源：{ev_status}", "",
             "掃描全部台股代號，列出原始收盤單日漲跌超過 10%（或還原後超過該檔漲跌幅限制）的紀錄。", ""]
    if len(rev):
        for cat, g in rev.groupby("category", sort=False):
            lines += [f"## {cat}（{len(g)} 筆）", "",
                      "| 代號 | 名稱 | 日期 | 前收 | 收盤 | 原始漲跌% | 還原後% | 來源 | 備註 |", "|---|---|---|---|---|---|---|---|---|"]
            for r in g.itertuples(index=False):
                lines.append(f"| {r.code} | {r.name} | {r.date} | {r.prev_close:g} | {r.close:g} | {r.ret_pct:+.2f} | "
                             f"{r.adj_ret_pct:+.2f} | {r.event_source} | {r.note} |")
            lines.append("")
    if mismatches:
        lines += ["## 事件前收盤與 CSV 不符", "", "```", json.dumps(mismatches, ensure_ascii=False, indent=1), "```"]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"寫入 {out}")


if __name__ == "__main__":
    main()
