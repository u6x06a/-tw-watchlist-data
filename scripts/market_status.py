#!/usr/bin/env python3
"""在 data/meta.json 寫入 markets 區塊：每個市場的「預期最新交易日」、實際資料日期與狀態。

狀態（status）：正常 / 休市 / 尚未公布 / 抓取失敗；reason 說明判斷依據。
  台股（TWSE、TPEX 分開）：日曆 = 證交所休市表 + XTAI + config calendar.tw_extra_closed。
    今天不是交易日 → 休市（資料應停在上一個交易日，否則抓取失敗）
    今天是交易日：有今天資料 → 正常；早於 tw_publish_after → 尚未公布（預期 = 上一個交易日）；
    今天的抓取有錯誤、或過了 tw_fail_after 仍無資料 → 抓取失敗；其餘 → 尚未公布
  美股（NYSE）：日曆 = XNYS。預期最新交易日 = 最近一個「已收盤」的交易日（盤中 K 棒已在 fetch_prices.py 排除）。
    資料 = 預期 → 正常（今天紐約休市時為 休市）；落後且收盤未滿 config calendar.us_grace_hours → 尚未公布；否則 抓取失敗。
每個市場都列出 data_matches_expected；不相符時下游不可把舊資料當成最新。

用法：python scripts/market_status.py   （在 fetch_prices.py 之後執行）
"""
import csv
import os
import sys
from collections import Counter
from datetime import time as dtime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, is_global, load_config, log, now_tw, path, read_json, write_json  # noqa: E402
from tw_calendar import previous_sessions  # noqa: E402

sys.path.insert(0, ROOT)


def _hm(s):
    h, m = map(int, str(s).split(":"))
    return dtime(h, m)


def tw_status(market, now, ss, counts, by_date, cfg_cal):
    today = now.date()
    today_iso = today.isoformat()
    sess_iso = [d.isoformat() for d in ss]
    is_sess = today_iso in sess_iso
    prev = [d for d in sess_iso if d < today_iso]
    last_prev = prev[-1] if prev else None
    # 實際資料日期：該市場筆數達近 20 個交易日中位數 80% 的最新日期（避免只有零星幾檔的日子）
    dates = sorted(counts)
    recent = [counts[d] for d in dates[-20:]]
    med = sorted(recent)[len(recent) // 2] if recent else 0
    full = [d for d in dates if counts[d] >= 0.8 * med]
    data_latest = full[-1] if full else None
    after_pub = now.time() >= _hm(cfg_cal["tw_publish_after"])
    after_fail = now.time() >= _hm(cfg_cal["tw_fail_after"])
    fetch_today = (by_date.get(today_iso) or {}).get(market)

    if not is_sess:
        expected = sess_iso[-1] if sess_iso else None
        status, reason = "休市", "今天不是台股交易日（證交所休市表 / XTAI / 臨時休市設定）"
    elif data_latest == today_iso:
        expected, status, reason = today_iso, "正常", "已取得今日資料"
    elif not after_pub:
        expected = last_prev
        status, reason = "尚未公布", f"今天是交易日，尚未到 {cfg_cal['tw_publish_after']}"
    elif fetch_today and fetch_today.startswith("error"):
        expected, status, reason = today_iso, "抓取失敗", f"今日抓取錯誤：{fetch_today[6:][:200]}"
    elif after_fail:
        expected, status = today_iso, "抓取失敗"
        reason = (f"超過 {cfg_cal['tw_fail_after']} 仍無今日資料（交易所回應：{fetch_today or '本次未抓'}）；"
                  "若確認為臨時休市，請加入 config calendar.tw_extra_closed")
    else:
        expected, status = today_iso, "尚未公布"
        reason = f"交易所尚未提供今日資料（{fetch_today or '本次未抓'}），{cfg_cal['tw_fail_after']} 前持續重試"
    # 休市 / 尚未公布時，資料至少要到上一個交易日
    need = expected if status in ("正常", "休市") else last_prev
    if status in ("休市", "尚未公布") and need and (data_latest or "") < need:
        status, reason = "抓取失敗", f"資料停在 {data_latest}，落後上一個交易日 {need}"
    return {"expected_latest": expected, "data_latest": data_latest,
            "data_matches_expected": data_latest == expected, "status": status, "reason": reason,
            "codes_on_latest": counts.get(data_latest, 0), "today_is_session": is_sess,
            "previous_session": last_prev}


def us_status(now, meta, cfg_cal):
    import pandas as pd
    from fetch_prices import GLOBAL, global_kind
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XNYS")
    except Exception as e:  # noqa: BLE001
        return {"status": "抓取失敗", "reason": f"XNYS 日曆不可用：{e}"}
    now_utc = now.astimezone(timezone.utc)
    ny_today = pd.Timestamp(now_utc).tz_convert("America/New_York").date()
    start = pd.Timestamp(ny_today - timedelta(days=15))
    sess = [s for s in cal.sessions_in_range(start, pd.Timestamp(ny_today))]
    closed_sess = [s for s in sess if cal.session_close(s).to_pydatetime() <= now_utc]
    expected = closed_sess[-1].date().isoformat() if closed_sess else None
    today_sess = pd.Timestamp(ny_today) in sess
    if today_sess:
        o, c = cal.session_open(pd.Timestamp(ny_today)), cal.session_close(pd.Timestamp(ny_today))
        state = "pre_open" if now_utc < o.to_pydatetime() else ("in_session" if now_utc < c.to_pydatetime() else "closed")
    else:
        state = "holiday_or_weekend"
    codes = [c for c in GLOBAL if global_kind(c) == "NYSE"]
    latest = meta.get("latest_date", {})
    per = {c: latest.get(c) for c in codes}
    stale = sorted(c for c, d in per.items() if (d or "") < (expected or ""))
    errs = {c: v for c, v in (meta.get("global_status") or {}).items() if c in codes and v != "ok"}
    data_latest = max((d for d in per.values() if d), default=None)
    if not stale:
        status = "休市" if state == "holiday_or_weekend" else "正常"
        reason = f"紐約時間 {ny_today}（{state}）；最近已收盤交易日 {expected}"
    else:
        last_close = cal.session_close(pd.Timestamp(expected)).to_pydatetime() if expected else now_utc
        within = (now_utc - last_close) < timedelta(hours=float(cfg_cal["us_grace_hours"]))
        status = "尚未公布" if within and not errs else "抓取失敗"
        reason = f"以下代號資料落後 {expected}：{stale}" + (f"；錯誤：{errs}" if errs else "")
    return {"expected_latest": expected, "data_latest": data_latest, "data_matches_expected": not stale,
            "status": status, "reason": reason, "session_state": state, "ny_date": ny_today.isoformat(),
            "stale_codes": stale, "partial_bars_excluded": {k: v for k, v in (meta.get("partial_bars") or {}).items()
                                                            if k in codes}}


def main():
    cfg = load_config()
    cfg_cal = cfg["calendar"]
    meta_p = path("data", "meta.json")
    meta = read_json(meta_p, {}) or {}
    now = now_tw()
    out = {"generated_at": now.isoformat(timespec="seconds")}
    try:
        ss, conflicts, cal_src = previous_sessions(30, now.date(), int(cfg_cal["holiday_cache_days"]),
                                                   float(cfg["data"]["request_sleep_sec"]),
                                                   cfg_cal.get("tw_extra_closed") or [])
        market_of = {}
        cp = path("data", "companies.csv")
        if os.path.exists(cp):
            with open(cp, newline="", encoding="utf-8") as f:
                market_of = {r["code"]: r["market"] for r in csv.DictReader(f)}
        counts = {"TWSE": Counter(), "TPEX": Counter(), "TW": Counter()}
        cutoff = (now.date() - timedelta(days=45)).isoformat()
        with open(path("data", "prices.csv"), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["date"] < cutoff or is_global(r["code"]):
                    continue
                counts["TW"][r["date"]] += 1
                m = market_of.get(r["code"])
                if m:
                    counts[m][r["date"]] += 1
        by_date = (meta.get("bulk") or {}).get("by_date") or {}
        for m in (("TWSE", "TPEX") if market_of else ("TW",)):
            out[m] = tw_status(m, now, ss, counts[m], by_date, cfg_cal)
        out["tw_calendar"] = {"source": cal_src, "xtai_vs_twse_conflicts": conflicts,
                              "extra_closed": cfg_cal.get("tw_extra_closed") or [],
                              "recent_sessions": [d.isoformat() for d in ss[-5:]]}
        if not market_of:
            out["note"] = "沒有 data/companies.csv，台股不分上市上櫃"
    except Exception as e:  # noqa: BLE001
        out["TW"] = {"status": "抓取失敗", "reason": f"market_status 錯誤：{type(e).__name__}: {e}"}
    try:
        out["NYSE"] = us_status(now, meta, cfg_cal)
    except Exception as e:  # noqa: BLE001
        out["NYSE"] = {"status": "抓取失敗", "reason": f"market_status 錯誤：{type(e).__name__}: {e}"}
    meta = read_json(meta_p, {}) or {}   # 重讀，避免覆蓋其他步驟剛寫入的內容
    meta["markets"] = out
    write_json(meta_p, meta)
    for k, v in out.items():
        if isinstance(v, dict) and "status" in v:
            log(f"{k}: {v['status']}  預期 {v.get('expected_latest')}  資料 {v.get('data_latest')}  {v.get('reason')}")


if __name__ == "__main__":
    main()
