#!/usr/bin/env python3
"""籌碼資料（不含券商分點）：外資持股比率、融資融券與借券賣出餘額、集保股權分散。

輸出（每檔都有資料日期欄）：
  存放：data/<名稱>.csv 為最近 config chips.recent_trading_days 個交易日；完整歷史（keep_trading_days）依月份存在
        data/history/<名稱>/YYYY-MM.csv（config chips.archive_history=false 時改為單一檔案）。集保為每週，單一檔案。
  data/foreign_holding.csv  每日。date,code,market,shares_issued,foreign_shares,foreign_pct,foreign_limit_pct,foreign_room_pct
                            foreign_pct 由本腳本以「外資及陸資持有股數 / 發行股數」計算（%，4 位小數）
  data/margin.csv           每日。date,code,market,margin_balance,margin_buy,margin_sell,margin_repay,margin_limit,
                            short_balance,short_sell,short_buy,short_repay,short_limit,offset,sbl_short_balance,sbl_short_sell,
                            sbl_short_return,sbl_short_adjust
                            融資/融券單位：張（交易單位）；借券賣出（sbl_*）單位：股（交易所原始單位）。
                            code=_MARKET 的列：margin_value_k = 全市場融資金額餘額（仟元），其餘欄位為合計張數。
  data/tdcc.csv             每週。date,code,holders,shares,pct_400plus,pct_1000plus,holders_400plus,holders_1000plus
                            date = 集保資料日期（每週最後一個營業日）。400 張以上 = 持股分級 12~15 級（400,001 股以上），
                            1000 張以上 = 第 15 級（1,000,001 股以上）；分級定義已於集保查詢頁核對。
                            比例由本腳本以各級股數 / 合計股數計算（%）。
來源（皆已實測，2026-10-15 資料）：
  外資持股  上市 https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?date=YYYYMMDD&selectType=ALLBUT0999&response=json
            上櫃 https://www.tpex.org.tw/www/zh-tw/insti/qfii?date=YYYY/MM/DD&response=json
  融資融券  上市 https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=YYYYMMDD&selectType=ALL&response=json
            上櫃 https://www.tpex.org.tw/www/zh-tw/margin/balance?date=YYYY/MM/DD&response=json
  借券賣出  上市 https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U?date=YYYYMMDD&response=json
            上櫃 https://www.tpex.org.tw/www/zh-tw/margin/sbl?date=YYYY/MM/DD&response=json
  集保      https://opendata.tdcc.com.tw/getOD.ashx?id=1-5（只有最新一週；歷史查詢頁需一檔一週逐筆查，不做全市場回補）

用法：python scripts/fetch_chips.py [--days N] [--only foreign|margin|tdcc]
"""
import csv
import io
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE_OK, Blocked, DailyStore, get, load_config, log, now_tw, num, path, roc_to_iso, write_json  # noqa: E402
from fetch_institutional import trading_dates  # noqa: E402

FH = path("data", "foreign_holding.csv")
MG = path("data", "margin.csv")
TD = path("data", "tdcc.csv")
FH_FIELDS = ["date", "code", "market", "shares_issued", "foreign_shares", "foreign_pct", "foreign_limit_pct",
             "foreign_room_pct"]
MG_FIELDS = ["date", "code", "market", "margin_balance", "margin_buy", "margin_sell", "margin_repay", "margin_limit",
             "short_balance", "short_sell", "short_buy", "short_repay", "short_limit", "offset",
             "sbl_short_balance", "sbl_short_sell", "sbl_short_return", "sbl_short_adjust", "margin_value_k"]
TD_FIELDS = ["date", "code", "holders", "shares", "pct_400plus", "pct_1000plus", "holders_400plus", "holders_1000plus"]


def i(x):
    v = num(str(x).replace("%", "")) if x is not None else None
    return None if v is None else int(v)


def f(x):
    return num(str(x).replace("%", "")) if x is not None else None


def ymd(d):
    return d.replace("-", "")


def slash(d):
    return d.replace("-", "/")


def tpex_table(j, d):
    t = next((t for t in j.get("tables", []) if t.get("data")), None)
    if str(j.get("stat", "ok")).lower() != "ok" or not t or roc_to_iso(t["date"]) != d:
        return None
    return t


# ---------------- 外資持股 ----------------
def foreign_twse(s, d, sleep):
    j = get(s, "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS",
            {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"}, sleep=sleep).json()
    if j.get("stat") != "OK" or str(j.get("date")) != ymd(d):
        return None
    fs = [x.strip() for x in j["fields"]]
    ix = {k: fs.index(k) for k in ("證券代號", "發行股數", "全體外資及陸資持有股數", "外資及陸資共用法令投資上限比率",
                                   "外資及陸資尚可投資比率")}
    out = []
    for r in j["data"]:
        code = str(r[ix["證券代號"]]).strip()
        if not CODE_OK.match(code):
            continue
        out.append(fh_row(d, code, "TWSE", i(r[ix["發行股數"]]), i(r[ix["全體外資及陸資持有股數"]]),
                          f(r[ix["外資及陸資共用法令投資上限比率"]]), f(r[ix["外資及陸資尚可投資比率"]])))
    return out


def foreign_tpex(s, d, sleep):
    j = get(s, "https://www.tpex.org.tw/www/zh-tw/insti/qfii", {"date": slash(d), "response": "json"}, sleep=sleep).json()
    t = tpex_table(j, d)
    if t is None:
        return None
    fs = [x.strip() for x in t["fields"]]

    def col(prefix):
        return next(k for k, x in enumerate(fs) if x.startswith(prefix))
    ic, ia, ih, il, ir = col("代號"), col("發行股數"), col("僑外資及陸資持有股數"), col("法令投資上限比率"), col("僑外資及陸資尚可投資比率")
    out = []
    for r in t["data"]:
        code = str(r[ic]).strip()
        if not CODE_OK.match(code):
            continue
        out.append(fh_row(d, code, "TPEX", i(r[ia]), i(r[ih]), f(r[il]), f(r[ir])))
    return out


def fh_row(d, code, market, issued, held, limit, room):
    return {"date": d, "code": code, "market": market, "shares_issued": issued, "foreign_shares": held,
            "foreign_pct": round(held / issued * 100, 4) if issued and held is not None else "",
            "foreign_limit_pct": limit, "foreign_room_pct": room}


# ---------------- 融資融券 + 借券賣出 ----------------
def margin_twse(s, d, sleep):
    j = get(s, "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
            {"date": ymd(d), "selectType": "ALL", "response": "json"}, sleep=sleep).json()
    if j.get("stat") != "OK" or str(j.get("date")) != ymd(d):
        return None
    tabs = j.get("tables", [])
    detail = next(t for t in tabs if t.get("fields") and t["fields"][0].strip() == "代號")
    summ = next(t for t in tabs if t.get("fields") and t["fields"][0].strip() == "項目")
    if len(detail["fields"]) < 15:
        raise ValueError(f"MI_MARGN 欄位數改變：{detail['fields']}")
    rows = {}
    for r in detail["data"]:
        code = str(r[0]).strip()
        if not CODE_OK.match(code):
            continue
        # 融資：2 買進 3 賣出 4 現金償還 5 前日餘額 6 今日餘額 7 限額；融券：8 買進 9 賣出 10 現券償還 11 前日 12 今日 13 限額；14 資券互抵
        rows[code] = {"date": d, "code": code, "market": "TWSE", "margin_buy": i(r[2]), "margin_sell": i(r[3]),
                      "margin_repay": i(r[4]), "margin_balance": i(r[6]), "margin_limit": i(r[7]),
                      "short_buy": i(r[8]), "short_sell": i(r[9]), "short_repay": i(r[10]),
                      "short_balance": i(r[12]), "short_limit": i(r[13]), "offset": i(r[14])}
    v = {str(r[0]).strip(): r for r in summ["data"]}
    mk = {"date": d, "code": "_MARKET", "market": "TWSE", "margin_balance": i(v["融資(交易單位)"][5]),
          "short_balance": i(v["融券(交易單位)"][5]), "margin_value_k": i(v["融資金額(仟元)"][5])}
    j = get(s, "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U", {"date": ymd(d), "response": "json"},
            sleep=sleep).json()
    if j.get("stat") == "OK" and str(j.get("date")) == ymd(d):
        add_sbl(rows, j["data"], d, "TWSE")
    return list(rows.values()) + [mk]


def add_sbl(rows, data, d, market):
    # TWT93U / 櫃買 sbl：0 代號 1 名稱，融券 2~7，借券賣出 8 前日餘額 9 當日賣出 10 當日還券 11 當日調整 12 當日餘額
    for r in data:
        code = str(r[0]).strip()
        if not CODE_OK.match(code):
            continue
        x = rows.setdefault(code, {"date": d, "code": code, "market": market})
        x.update({"sbl_short_sell": i(r[9]), "sbl_short_return": i(r[10]), "sbl_short_adjust": i(r[11]),
                  "sbl_short_balance": i(r[12])})


def margin_tpex(s, d, sleep):
    j = get(s, "https://www.tpex.org.tw/www/zh-tw/margin/balance", {"date": slash(d), "response": "json"},
            sleep=sleep).json()
    t = tpex_table(j, d)
    if t is None:
        return None
    if len(t["fields"]) < 19:
        raise ValueError(f"櫃買融資融券欄位數改變：{t['fields']}")
    rows = {}
    for r in t["data"]:
        code = str(r[0]).strip()
        if not CODE_OK.match(code):
            continue
        # 2 前資餘額 3 資買 4 資賣 5 現償 6 資餘額 7 資屬證金 8 資使用率 9 資限額 10 前券 11 券賣 12 券買 13 券償 14 券餘額 ... 17 券限額 18 資券相抵
        rows[code] = {"date": d, "code": code, "market": "TPEX", "margin_buy": i(r[3]), "margin_sell": i(r[4]),
                      "margin_repay": i(r[5]), "margin_balance": i(r[6]), "margin_limit": i(r[9]),
                      "short_sell": i(r[11]), "short_buy": i(r[12]), "short_repay": i(r[13]),
                      "short_balance": i(r[14]), "short_limit": i(r[17]), "offset": i(r[18])}
    summ = {str(r[1]).strip(): r for r in t.get("summary") or []}
    mk = {"date": d, "code": "_MARKET", "market": "TPEX",
          "margin_balance": i(summ["合計(張)"][6]) if "合計(張)" in summ else None,
          "short_balance": i(summ["合計(張)"][14]) if "合計(張)" in summ else None,
          "margin_value_k": i(summ["融資金(仟元)"][6]) if "融資金(仟元)" in summ else None}
    j = get(s, "https://www.tpex.org.tw/www/zh-tw/margin/sbl", {"date": slash(d), "response": "json"}, sleep=sleep).json()
    t2 = tpex_table(j, d)
    if t2 is not None:
        add_sbl(rows, t2["data"], d, "TPEX")
    return list(rows.values()) + [mk]


# ---------------- 共用：按日期更新（近期檔 + 分月歷史，見 common.DailyStore） ----------------
def daily_dataset(name, fname, fields, fns, targets, recent, c, sleep, s):
    store = DailyStore(fname, fields, ("date", "market", "code"), int(c["recent_trading_days"]),
                       int(c["keep_trading_days"]), archive=bool(c["archive_history"]),
                       long_codes=("_MARKET",), long_days=int(c["keep_trading_days"]))
    store.load_months(targets)
    st = {}
    try:
        for d in targets:
            st[d] = {}
            for market, fn in fns:
                if store.has(d, market) and d not in recent:
                    st[d][market] = "cached"
                    continue
                try:
                    rows = fn(s, d, sleep)
                    if rows is None:
                        st[d][market] = "pending（交易所尚未公布）"
                        continue
                    store.replace(d, market, rows)
                    st[d][market] = f"ok:{len(rows)}"
                except Blocked as e:
                    st[d][market] = f"blocked: {e}"
                    log(f"{name} {d} 被擋，存檔後停止（不重試）：{e}")
                    raise
                except Exception as e:  # noqa: BLE001
                    st[d][market] = f"error: {type(e).__name__}: {e}"
            log(f"{name} {d} {st[d]}")
    finally:
        n = store.save()
    latest = {m: max((k[0] for k, r in store.rows.items() if r.get("market") == m), default=None)
              for m in ("TWSE", "TPEX")}
    return {"latest_date": latest, "recent_file_days": n, "by_date": st}

def tdcc(s, sleep, keep_weeks):
    raw = get(s, "https://opendata.tdcc.com.tw/getOD.ashx", {"id": "1-5"}, sleep=sleep).content.decode("utf-8-sig")
    agg = {}
    for r in csv.DictReader(io.StringIO(raw)):
        code = r["證券代號"].strip()
        if not CODE_OK.match(code):
            continue
        d = r["資料日期"].strip()
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        lv = int(r["持股分級"])
        a = agg.setdefault((d, code), {"lv": {}})
        a["lv"][lv] = (int(r["人數"] or 0), int(r["股數"] or 0))
    rows = {}
    for (d, code), a in agg.items():
        lv = a["lv"]
        total = lv.get(17) or lv.get(16)   # opendata：16 = 差異數調整、17 = 合計
        if not total or not total[1]:
            continue
        big4 = [lv.get(k, (0, 0)) for k in (12, 13, 14, 15)]
        big10 = lv.get(15, (0, 0))
        rows[(d, code)] = {"date": d, "code": code, "holders": total[0], "shares": total[1],
                           "pct_400plus": round(sum(x[1] for x in big4) / total[1] * 100, 4),
                           "pct_1000plus": round(big10[1] / total[1] * 100, 4),
                           "holders_400plus": sum(x[0] for x in big4), "holders_1000plus": big10[0]}
    have = {}
    if os.path.exists(TD):
        with open(TD, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                have[(r["date"], r["code"])] = r
    have.update(rows)
    weeks = sorted({k[0] for k in have})[-keep_weeks:]
    keep = set(weeks)
    tmp = TD + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TD_FIELDS)
        w.writeheader()
        for k in sorted(have):
            if k[0] in keep:
                w.writerow(have[k])
    os.replace(tmp, TD)
    week = max(k[0] for k in rows) if rows else None
    return {"latest_week": week, "codes": len(rows), "weeks_kept": len(weeks)}


def main():
    cfg = load_config()
    c = cfg["chips"]
    sleep = float(cfg["data"]["request_sleep_sec"])
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    n = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else int(c["refetch_recent_days"])
    tds = trading_dates()
    targets = tds[-n:]
    recent = set(tds[-int(c["refetch_recent_days"]):])
    s = requests.Session()
    status = {"generated_at": now_tw().isoformat(timespec="seconds"),
              "expected_latest_trading_date": tds[-1] if tds else None}
    blocked = None
    try:
        if only in (None, "foreign"):
            status["foreign_holding"] = daily_dataset("外資持股", "foreign_holding", FH_FIELDS,
                                                      (("TWSE", foreign_twse), ("TPEX", foreign_tpex)),
                                                      targets, recent, c, sleep, s)
        if only in (None, "margin"):
            status["margin"] = daily_dataset("融資融券", "margin", MG_FIELDS, (("TWSE", margin_twse), ("TPEX", margin_tpex)),
                                             targets, recent, c, sleep, s)
        if only in (None, "tdcc"):
            try:
                status["tdcc"] = tdcc(s, sleep, int(c["tdcc_keep_weeks"]))
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                status["tdcc"] = {"error": f"{type(e).__name__}: {e}"}
    except Blocked as e:
        blocked = str(e)
        status["blocked"] = blocked
    old = {}
    sp = path("data", "chips_status.json")
    if os.path.exists(sp):
        import json
        old = json.load(open(sp, encoding="utf-8"))
    old.update(status)
    write_json(sp, old)
    log({k: (v.get("latest_date") or v.get("latest_week") or v) if isinstance(v, dict) else v for k, v in status.items()})
    if blocked:
        log(f"被擋，停止（不重試）：{blocked}")
        sys.exit(2)


if __name__ == "__main__":
    main()
