#!/usr/bin/env python3
"""上市、上櫃公司月營收 -> data/revenue.csv（保留最近 config revenue.months 個月）。

欄位（金額單位：千元，與來源相同）：
  code,market,ym,revenue_k,revenue_prev_month_k,revenue_last_year_k,mom_pct,yoy_pct,
  cum_revenue_k,cum_revenue_last_year_k,cum_yoy_pct,note,source,source_date
  ym            營收所屬月份（YYYY-MM）
  mom_pct / yoy_pct / cum_yoy_pct   由本腳本以營收數字計算（%，四捨五入 2 位；分母為 0 或缺值時留空）
  source_date   來源出表日期（opendata CSV）；MOPS 彙總頁沒有出表日期，填抓取日期
來源（已實測，2026-10-16）：
  最新月份：https://mopsfin.twse.com.tw/opendata/t187ap05_L.csv（上市）、t187ap05_O.csv（上櫃）
  歷史月份：https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{民國年}_{月}_{0|1}.html
           （sii 上市 / otc 上櫃；_0 國內公司、_1 國外公司 KY；Big5 編碼）
更新時機：每月 1 日到 config revenue.daily_until_day 每天抓最新月份（公司陸續公布）；
         缺少的歷史月份只在加 --history 時抓 MOPS 彙總頁（每月 4 個請求）。

用法：python scripts/fetch_revenue.py [--months N] [--history]
  --history  補缺少的歷史月份（MOPS 彙總頁）；每日流程不加，只抓最新月份
"""
import csv
import io
import os
import re
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE_OK, Blocked, get, load_config, log, now_tw, num, path, roc_to_iso, write_json  # noqa: E402

OUT = path("data", "revenue.csv")
FIELDS = ["code", "market", "ym", "revenue_k", "revenue_prev_month_k", "revenue_last_year_k", "mom_pct", "yoy_pct",
          "cum_revenue_k", "cum_revenue_last_year_k", "cum_yoy_pct", "note", "source", "source_date"]
MIN_ROWS_MONTH = 1500   # 某月已有這麼多家公司 = 視為完整（上市約 1,080 + 上櫃約 880）
ROW_RE = re.compile(r"<tr align=right><td align=center>\s*([0-9A-Z]{4,6})\s*</td><td align=left>(.*?)</td>(.*?)</tr>",
                    re.S | re.I)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def pct(a, b):
    if a is None or b in (None, 0):
        return ""
    return round((a / b - 1) * 100, 2)


def make_row(code, market, ym, rev, prev, ly, cum, cum_ly, note, source, sdate):
    return {"code": code, "market": market, "ym": ym, "revenue_k": rev, "revenue_prev_month_k": prev,
            "revenue_last_year_k": ly, "mom_pct": pct(rev, prev), "yoy_pct": pct(rev, ly),
            "cum_revenue_k": cum, "cum_revenue_last_year_k": cum_ly, "cum_yoy_pct": pct(cum, cum_ly),
            "note": (note or "").strip().replace("\n", " ")[:200], "source": source, "source_date": sdate}


def fetch_latest_csv(s, sleep):
    out = []
    for market, suf in (("TWSE", "L"), ("TPEX", "O")):
        raw = get(s, f"https://mopsfin.twse.com.tw/opendata/t187ap05_{suf}.csv", sleep=sleep).content
        text = raw.decode("utf-8-sig", errors="strict")
        for r in csv.DictReader(io.StringIO(text)):
            code = r["公司代號"].strip()
            if not CODE_OK.match(code):
                continue
            ym_roc = r["資料年月"].strip()           # 11509
            ym = f"{int(ym_roc[:-2]) + 1911:04d}-{ym_roc[-2:]}"
            out.append(make_row(code, market, ym, num(r["營業收入-當月營收"]), num(r["營業收入-上月營收"]),
                                num(r["營業收入-去年當月營收"]), num(r["累計營業收入-當月累計營收"]),
                                num(r["累計營業收入-去年累計營收"]), r.get("備註", "").strip("- "),
                                f"t187ap05_{suf}", roc_to_iso(r["出表日期"])))
    return out


def fetch_month_html(s, y, m, sleep):
    """MOPS 彙總頁：某月上市 + 上櫃（國內 + KY）全部公司。"""
    out, today = [], now_tw().date().isoformat()
    ym = f"{y:04d}-{m:02d}"
    for market, mk in (("TWSE", "sii"), ("TPEX", "otc")):
        for kind in (0, 1):
            url = f"https://mopsov.twse.com.tw/nas/t21/{mk}/t21sc03_{y - 1911}_{m}_{kind}.html"
            try:
                html = get(s, url, sleep=sleep).content.decode("big5", errors="replace")
            except requests.HTTPError as e:
                if kind == 1 and e.response is not None and e.response.status_code == 404:
                    continue
                raise
            for code, _name, rest in ROW_RE.findall(html):
                cells = [re.sub(r"<[^>]+>|&nbsp;", "", c).strip() for c in CELL_RE.findall(rest)]
                if len(cells) < 8 or not CODE_OK.match(code):
                    continue
                note = cells[8] if len(cells) > 8 else ""
                out.append(make_row(code, market, ym, num(cells[0]), num(cells[1]), num(cells[2]), num(cells[5]),
                                    num(cells[6]), "" if note == "-" else note, f"mops_t21sc03_{mk}_{kind}", today))
    return out


def month_list(n, today):
    """最近 n 個「已結束」月份，由新到舊。"""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return out


def main():
    cfg = load_config()["revenue"]
    sleep = float(load_config()["data"]["request_sleep_sec"])
    n = int(sys.argv[sys.argv.index("--months") + 1]) if "--months" in sys.argv else int(cfg["months"])
    today = now_tw().date()
    have = {}
    if os.path.exists(OUT):
        with open(OUT, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                have[(r["code"], r["ym"])] = r
    count = {}
    for (_, ym) in have:
        count[ym] = count.get(ym, 0) + 1
    s = requests.Session()
    status = {}
    blocked = None
    history = "--history" in sys.argv   # 歷史月份只在補資料時抓（每日流程只抓最新月份）

    months = month_list(n, today)
    keep = {f"{y:04d}-{m:02d}" for y, m in months}
    # 1. 最新月份：公布期間每天抓 opendata CSV
    if today.day <= int(cfg["daily_until_day"]) or count.get(f"{months[0][0]:04d}-{months[0][1]:02d}", 0) < MIN_ROWS_MONTH:
        try:
            rows = fetch_latest_csv(s, sleep)
            for r in rows:
                have[(r["code"], r["ym"])] = r
            yms = sorted({r["ym"] for r in rows})
            status["latest_csv"] = f"ok（{len(rows)} 筆，月份 {yms}）"
        except Blocked as e:
            status["latest_csv"] = f"blocked: {e}"
            blocked = str(e)
        except Exception as e:  # noqa: BLE001
            status["latest_csv"] = f"{type(e).__name__}: {e}"
    count = {}
    for (_, ym) in have:
        count[ym] = count.get(ym, 0) + 1
    # 2. 缺的歷史月份：MOPS 彙總頁（最新月份不在此補，避免覆蓋 CSV 的出表日期）
    for y, m in months[1:]:
        ym = f"{y:04d}-{m:02d}"
        if blocked or not history or count.get(ym, 0) >= MIN_ROWS_MONTH:
            continue
        try:
            rows = fetch_month_html(s, y, m, sleep)
            for r in rows:
                have[(r["code"], r["ym"])] = r
            status[ym] = f"ok（{len(rows)} 筆）"
            log(f"{ym}: {len(rows)} 筆")
        except Blocked as e:   # 被擋：不重試、不再送請求
            status[ym] = f"blocked: {e}"
            blocked = str(e)
        except Exception as e:  # noqa: BLE001
            status[ym] = f"{type(e).__name__}: {e}"
    rows = [have[k] for k in sorted(have) if k[1] in keep]
    tmp = OUT + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUT)
    by_month = {}
    for r in rows:
        by_month[r["ym"]] = by_month.get(r["ym"], 0) + 1
    write_json(path("data", "revenue_status.json"),
               {"generated_at": now_tw().isoformat(timespec="seconds"), "latest_ym": max(by_month) if by_month else None,
                "rows_by_month": dict(sorted(by_month.items())), "sources": status})
    log(f"寫入 {OUT}：{len(rows)} 筆；{status}")
    if blocked:
        log(f"被擋，停止（不重試）：{blocked}")
        sys.exit(2)


if __name__ == "__main__":
    main()
