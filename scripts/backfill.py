#!/usr/bin/env python3
"""補歷史資料（階段 1-4）。抓取函式沿用 fetch_prices.py（證交所 MI_INDEX、櫃買 afterTrading/otc 單日全市場）。

子命令：
  plan                 列出計畫：要抓的交易日、請求數、預估時間（只讀休市日曆，不抓價）
  prices [--with-ref] [--max-days N]
                       把全市場補到 config backfill.target_trading_days 個交易日（由舊到新抓，每 20 天存檔一次）。
                       --with-ref：已有價格、但還沒用漲跌欄檢查過參考價的日期也重抓（只更新 ref_events，價格以新抓為準）
  ref --dates D1,D2 | --from-review
                       只重抓指定日期，更新 data/ref_events.csv，並把重抓結果與 prices.csv 既有值比對，
                       差異寫到 data/refetch_diff.csv（用來確認壞資料）。不修改 prices.csv。
                       --from-review：日期取自 data/jump_review.csv
每個交易日 2 個請求（上市、上櫃），每次間隔 config data.request_sleep_sec 秒。
"""
import csv
import os
import sys
import time
from datetime import timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, is_global, load_config, log, now_tw, path, write_json  # noqa: E402
from tw_calendar import previous_sessions  # noqa: E402

sys.path.insert(0, ROOT)
import fetch_prices as fp  # noqa: E402
import refprice  # noqa: E402

SEC_PER_DAY_EST = 2 * (1.5 + fp.SLEEP)   # 每個交易日：2 個請求 ×（回應約 1.5 秒 + 間隔）


def tw_counts(merged):
    c = {}
    for (code, d) in merged:
        if not is_global(code):
            c[d] = c.get(d, 0) + 1
    return c


def write_prices(merged):
    """與 fetch_prices.py 相同的保留規則與格式。"""
    today = now_tw().date()
    cutoff = (today - timedelta(days=fp.KEEP_DAYS)).isoformat()
    cutoff_tracked = (today - timedelta(days=fp.KEEP_DAYS_TRACKED)).isoformat()
    keys = sorted(k for k in merged
                  if k[1] >= (cutoff_tracked if (k[0] in fp.WATCHLIST or k[0] in fp.GLOBAL) else cutoff))
    tmp = fp.PRICES_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fp.FIELDS)
        for (c, d) in keys:
            w.writerow([c, d, *merged[(c, d)]])
    os.replace(tmp, fp.PRICES_CSV)
    return len(keys)


def coverage():
    cov = {}
    if os.path.exists(refprice.COVERAGE):
        import json
        cov = json.load(open(refprice.COVERAGE, encoding="utf-8"))
    return {m: set(v) for m, v in cov.items()}


def build_plan(cfg, with_ref, max_days=None):
    n = int(cfg["backfill"]["target_trading_days"])
    ss, conflicts, st = previous_sessions(n, None, int(cfg["calendar"]["holiday_cache_days"]),
                                          float(cfg["data"]["request_sleep_sec"]),
                                          cfg["calendar"].get("tw_extra_closed") or [])
    merged = fp.load_existing()
    counts = tw_counts(merged)
    cov = coverage()
    today = now_tw().date()
    oldest_kept = today - timedelta(days=fp.KEEP_DAYS)
    missing, ref_only, too_old = [], [], []
    for d in ss:
        iso = d.isoformat()
        if d < oldest_kept:
            too_old.append(iso)
            continue
        if counts.get(iso, 0) < fp.COMPLETE_MIN_ROWS:
            missing.append(d)
        elif with_ref and not (iso in cov.get("TWSE", set()) and iso in cov.get("TPEX", set())):
            ref_only.append(d)
    todo = sorted(missing + ref_only)
    if max_days:
        todo = todo[:max_days]
    return {"sessions": ss, "missing": missing, "ref_only": ref_only, "too_old_for_keep_days": too_old,
            "todo": todo, "calendar_source": st, "calendar_conflicts": conflicts, "merged": merged}


def cmd_plan(cfg):
    p = build_plan(cfg, with_ref=True)
    ss = p["sessions"]
    req = 2 * len(p["todo"])
    print(f"目標：最近 {len(ss)} 個台股交易日（{ss[0]} ~ {ss[-1]}）；日曆來源 {p['calendar_source']}")
    print(f"  缺價格的交易日：{len(p['missing'])}（{p['missing'][0] if p['missing'] else '-'} ~ "
          f"{p['missing'][-1] if p['missing'] else '-'}）")
    print(f"  已有價格但未檢查參考價（--with-ref 才抓）：{len(p['ref_only'])}")
    print(f"  超出 keep_calendar_days_all（{fp.KEEP_DAYS} 天）而不抓：{len(p['too_old_for_keep_days'])}")
    print(f"  請求數：只補缺 {2 * len(p['missing'])}；含參考價 {req}")
    print(f"  預估時間：只補缺 {2 * len(p['missing']) * SEC_PER_DAY_EST / 2 / 60:.0f} 分；"
          f"含參考價 {req * SEC_PER_DAY_EST / 2 / 60:.0f} 分（間隔 {fp.SLEEP} 秒）")
    if p["calendar_conflicts"]:
        print(f"  日曆不一致：{p['calendar_conflicts']}")


def fetch_days(days, merged, write_back, diffs=None):
    """逐日抓取並合併（全市場來源覆蓋既有列，統一成交量口徑）。write_back=True 時每 20 天寫回 prices.csv 並更新參考價。
    遇到 403/429 或被擋（fetch_prices.Blocked）立刻存檔並以代碼 2 結束，不重試。
    diffs：統計被覆蓋的既有列與新值的差異 {欄位: 筆數}，以及差異範例。回傳 (bulk_keys, 每日結果)。"""
    s = requests.Session()
    raw_batch, result, bulk_keys = [], {}, set()
    diffs = diffs if diffs is not None else {}
    t0 = time.time()

    def save():
        n = write_prices(merged)
        ev = refprice.update(merged, raw_batch, complete_min_rows=fp.COMPLETE_MIN_ROWS)
        fp.update_fallback_rows({}, bulk_keys, set(merged))
        log(f"  存檔：prices.csv {n} 筆；參考價事件 +{ev}")
        raw_batch.clear()

    for i, d in enumerate(days, 1):
        res = fp.fetch_bulk_day(s, d)
        st = {}
        blocked = None
        for market, (rows, err) in res.items():
            st[market] = f"error:{err}" if err else (f"ok:{len(rows)}" if rows else "no_data")
            if err and err.startswith("Blocked"):
                blocked = err
            for r in rows or []:
                k = (r["code"], r["date"])
                old = merged.get(k)
                if old is not None:
                    for j, fld in enumerate(("open", "high", "low", "close", "volume")):
                        try:
                            a, b = float(old[j]), float(r[fld])
                        except (TypeError, ValueError):
                            continue
                        if abs(a - b) > (0.5 if fld == "volume" else 0.0051):
                            diffs[fld] = diffs.get(fld, 0) + 1
                            ex = diffs.setdefault("examples", [])
                            if len(ex) < 30:
                                ex.append([k[0], k[1], fld, old[j], r[fld]])
                if write_back:
                    fp.put(merged, r)
                bulk_keys.add(k)
                if "_chg" in r:
                    raw_batch.append(r)
        result[d.isoformat()] = st
        eta = (time.time() - t0) / i * (len(days) - i) / 60
        log(f"[{i}/{len(days)}] {d} {st}  剩餘約 {eta:.0f} 分")
        if blocked:
            if write_back:
                save()
            write_json(path("data", "backfill_status.json"),
                       {"generated_at": now_tw().isoformat(timespec="seconds"), "stopped": f"{d}: {blocked}",
                        "fetched_days": len(result), "by_date": result})
            log(f"被交易所擋下，已存檔並停止（不重試）：{blocked}")
            sys.exit(2)
        if write_back and (i % 20 == 0 or i == len(days)):
            save()
    return bulk_keys, result


def cmd_prices(cfg, with_ref, max_days):
    p = build_plan(cfg, with_ref, max_days)
    log(f"要抓 {len(p['todo'])} 個交易日（缺價 {len(p['missing'])}、參考價 {len(p['ref_only'])}）")
    diffs = {}
    bulk_keys, result = fetch_days(p["todo"], p["merged"], write_back=True, diffs=diffs)
    bad = {d: v for d, v in result.items() if any(not x.startswith("ok") for x in v.values())}
    # 沒有任何全市場來源的台股列（早於全市場資料、只有逐檔來源的關注名單舊資料）→ 標記為備援來源
    cov = coverage()
    covered = set().union(*cov.values()) if cov else set()
    merged = fp.load_existing()
    legacy = {k: "per_stock_legacy" for k in merged if not is_global(k[0]) and k[1] not in covered}
    n_fb = fp.update_fallback_rows(legacy, bulk_keys, set(merged))
    write_json(path("data", "backfill_status.json"),
               {"generated_at": now_tw().isoformat(timespec="seconds"), "target_trading_days": len(p["sessions"]),
                "fetched_days": len(result), "not_ok": bad, "calendar_conflicts": p["calendar_conflicts"],
                "replaced_existing_diffs": diffs, "fallback_rows_after": n_fb})
    log(f"完成；非 ok 的日子：{bad}；覆蓋既有列的差異 { {k: v for k, v in diffs.items() if k != 'examples'} }；"
        f"備援來源列 {n_fb}")


def cmd_ref(dates):
    from datetime import date
    merged = fp.load_existing()
    days = sorted({date.fromisoformat(x) for x in dates})
    fresh = {}
    s = requests.Session()
    raw = []
    for i, d in enumerate(days, 1):
        res = fp.fetch_bulk_day(s, d)
        for market, (rows, err) in res.items():
            for r in rows or []:
                fresh[(r["code"], r["date"])] = r
                if "_chg" in r:
                    raw.append(r)
        log(f"[{i}/{len(days)}] {d} " + str({m: (len(v[0]) if v[0] else v[1] or 'no_data') for m, v in res.items()}))
    # 與既有 prices.csv 比對
    diffs = []
    for k, r in fresh.items():
        old = merged.get(k)
        if old is None:
            diffs.append({"code": k[0], "date": k[1], "field": "row", "stored": "", "refetched": "present"})
            continue
        for i, fld in enumerate(("open", "high", "low", "close", "volume")):
            try:
                a, b = float(old[i]), float(r[fld])
            except (TypeError, ValueError):
                continue
            if abs(a - b) > (0.5 if fld == "volume" else 0.0051):
                diffs.append({"code": k[0], "date": k[1], "field": fld, "stored": old[i], "refetched": r[fld]})
    # 參考價：前一日收盤用既有資料，當日用新抓資料
    view = dict(merged)
    for k, r in fresh.items():
        view[k] = [r["open"], r["high"], r["low"], r["close"], r["volume"]]
    ev = refprice.update(view, raw, source="refetch", complete_min_rows=fp.COMPLETE_MIN_ROWS)
    with open(path("data", "refetch_diff.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "date", "field", "stored", "refetched"])
        w.writeheader()
        w.writerows(diffs)
    log(f"重抓 {len(days)} 日；參考價事件 {ev} 筆；與既有資料差異 {len(diffs)} 筆（data/refetch_diff.csv）")


def main():
    cfg = load_config()
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "plan":
        cmd_plan(cfg)
    elif cmd == "prices":
        mx = int(sys.argv[sys.argv.index("--max-days") + 1]) if "--max-days" in sys.argv else None
        cmd_prices(cfg, "--with-ref" in sys.argv, mx)
    elif cmd == "ref":
        if "--from-review" in sys.argv:
            with open(path("data", "jump_review.csv"), newline="", encoding="utf-8") as f:
                dates = sorted({r["date"] for r in csv.DictReader(f)})
        else:
            dates = sys.argv[sys.argv.index("--dates") + 1].split(",")
        cmd_ref(dates)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
