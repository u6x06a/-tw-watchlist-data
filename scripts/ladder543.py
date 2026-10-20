#!/usr/bin/env python3
"""543 階梯佈局法（蔡明翰）：以 0050 逐日回放，輸出每日的階梯狀態。

規則（參數全部在 config/rules.yaml 的 market.ladder）
  5：資金拆成 tranches（預設 5）等份，每次進場投入 1 份。
  第 1 筆：符合下列任一條件的當日收盤進場（訊號在收盤產生）
    A 帶量跌破整理區間：收盤 < 前 range_days 日最低價，
      且前 range_days 日的最高 / 最低 − 1 ≤ max_range_pct（確實在整理，不是主跌段中的新低），
      且當日成交量 ≥ 前 vol_days 日均量 × vol_mult。
    B 恐慌性急跌：單日跌幅 ≥ panic.daily_drop_pct，且成交量 ≥ 前 vol_days 日均量 × panic.vol_mult。
  第 2~5 筆：進場後遇震盪不出手，收盤跌破「前一筆進場成本」add_drop_pct% 才加下一筆；一天最多加一筆。
結束一輪（本檔的補充假設，不在原規則內）：收盤回到整理區間上緣（第 1 筆當日之前 range_days 日最高價）。
  這個結束條件是資料端的假設，用來讓模擬帳知道何時換回 0050；可在 reset.mode 改為 never（不重置，只能手動）。
進場成本 = 觸發當日收盤價（還原價）；實際成交價會不同。
"""
import numpy as np


def _vr(v, i, n):
    """當日量 / 前 n 日均量（不含當日）；資料不足或有缺值回傳 None。"""
    if i < n:
        return None
    seg = v[i - n:i]
    if np.isnan(seg).any() or np.isnan(v[i]):
        return None
    m = float(np.mean(seg))
    return float(v[i] / m) if m > 0 else None


def run_ladder(dates, c, h, l, v, p):
    """dates/c/h/l/v：0050 的逐日序列（c、h、l 還原價；v 成交量）。p = config market.ladder。
    回傳每日一筆 dict；沒有收盤價的日子為空 dict。"""
    n = len(c)
    N = int(p["tranches"])
    add = float(p["add_drop_pct"]) / 100
    bk, pn = p["breakdown"], p["panic"]
    R, VD = int(bk["range_days"]), int(bk["vol_days"])
    reset_mode = (p.get("reset") or {}).get("mode", "reclaim_range_high")
    chg = np.r_[np.nan, c[1:] / c[:-1] - 1] * 100
    rows = []
    active, level, last_cost, range_high, steps, start = False, 0, None, None, [], None
    for i in range(n):
        if np.isnan(c[i]):
            rows.append({})
            continue
        new, ended = [], False
        # 觀察值（未啟動時顯示「離觸發還差多少」）
        watch = None
        if i >= R:
            lo, hi = float(np.nanmin(l[i - R:i])), float(np.nanmax(h[i - R:i]))
            rng = (hi / lo - 1) * 100
            vr = _vr(v, i, VD)
            below = bool(c[i] < lo)
            brk = bool(below and rng <= float(bk["max_range_pct"]) and vr is not None and vr >= float(bk["vol_mult"]))
            pan = bool(chg[i] <= -float(pn["daily_drop_pct"]) and vr is not None and vr >= float(pn["vol_mult"]))
            watch = {"range_low": round(lo, 2), "range_high": round(hi, 2), "range_pct": round(rng, 2),
                     "below_range_low": below, "vol_ratio": None if vr is None else round(vr, 2),
                     "breakdown_ok": brk, "panic_ok": pan}
        if active:
            if reset_mode == "reclaim_range_high" and c[i] >= range_high:
                active, level, last_cost, ended = False, 0, None, True
                steps, start = [], None
            elif level < N and c[i] <= last_cost * (1 - add):
                drop = (c[i] / last_cost - 1) * 100
                level += 1
                st = {"level": level, "date": dates[i], "type": "add", "price": round(float(c[i]), 2),
                      "drop_from_prev_pct": round(float(drop), 2)}
                steps = steps + [st]
                new.append(st)
                last_cost = float(c[i])
        elif watch and (watch["breakdown_ok"] or watch["panic_ok"]):
            typ = "breakdown+panic" if (watch["breakdown_ok"] and watch["panic_ok"]) else ("breakdown" if watch["breakdown_ok"] else "panic")
            active, level, last_cost, range_high, start = True, 1, float(c[i]), watch["range_high"], dates[i]
            st = {"level": 1, "date": dates[i], "type": typ, "price": round(float(c[i]), 2), "vol_ratio": watch["vol_ratio"],
                  "range_pct": watch["range_pct"], "change_pct": round(float(chg[i]), 2) if not np.isnan(chg[i]) else None}
            steps = [st]
            new.append(st)
        rows.append({"date": dates[i], "active": active, "level": level, "max_level": N, "start_date": start,
                     "steps": steps, "new": new, "ended_today": ended, "last_cost": last_cost,
                     "next_add_price": (round(last_cost * (1 - add), 2) if active and level < N else None),
                     "range_high": range_high if active else None, "watch": watch})
    return rows
