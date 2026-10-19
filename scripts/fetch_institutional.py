#!/usr/bin/env python3
"""三大法人買賣超（個股與全市場）-> data/institutional.csv。

欄位：date,code,market,unit,foreign_net,foreign_dealer_net,trust_net,dealer_net,total_net
  個股列：unit=shares（股）。foreign_net = 外陸資（不含外資自營商）；foreign_dealer_net = 外資自營商；
         dealer_net = 自營商合計（自行買賣 + 避險）；total_net = 交易所公布的三大法人合計。
  全市場列：code=_MARKET，unit=TWD（元），同樣分項（交易所的金額統計表）。
  date 即資料日期（交易所資料所屬交易日）。
來源（已實測，2026-10-15 資料）：
  上市個股  https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json
  上市合計  https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=YYYYMMDD&response=json
  上櫃個股  https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=YYYY/MM/DD&response=json
  上櫃合計  https://www.tpex.org.tw/www/zh-tw/insti/summary?type=Daily&date=YYYY/MM/DD&response=json
  櫃買個股表的欄位名稱沒有分組標題（買進/賣出/買賣超 重複 7 組），依序為：外資不含自營、外資自營、外資合計、
  投信、自營自行、自營避險、自營合計、三大法人合計。程式逐列檢查「外資合計 = 前兩組」「自營合計 = 自行 + 避險」
  「三大法人 = 外資合計 + 投信 + 自營合計」，不成立就整天標錯誤、不寫入。
T86 約在收盤後 16:00 前後公布；尚未公布時狀態記為 pending，下一次執行再抓。

用法：python scripts/fetch_institutional.py [--days N]   （--days：補最近 N 個交易日，預設 config refetch_recent_days）
"""
import csv
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE_OK, Blocked, DailyStore, get, load_config, log, now_tw, num, path, roc_to_iso, write_json  # noqa: E402

OUT = path("data", "institutional.csv")
FIELDS = ["date", "code", "market", "unit", "foreign_net", "foreign_dealer_net", "trust_net", "dealer_net", "total_net",
          "foreign_net_lots", "trust_net_lots", "dealer_net_lots", "total_net_lots", "close",
          "foreign_net_value_est", "trust_net_value_est", "dealer_net_value_est", "total_net_value_est"]
# *_lots = 股數 / 1000（張）。*_value_est = 買賣超股數 × 當日收盤價（元）：交易所個股表只公布股數，
# 這是以收盤價換算的近似金額，不是實際成交金額；全市場列（_MARKET）的金額欄才是交易所公布的實際金額。


def i(x):
    v = num(x)
    return None if v is None else int(v)


def twse_stock(s, d, sleep):
    j = get(s, "https://www.twse.com.tw/rwd/zh/fund/T86",
            {"date": d.replace("-", ""), "selectType": "ALLBUT0999", "response": "json"}, sleep=sleep).json()
    if j.get("stat") != "OK" or str(j.get("date")) != d.replace("-", ""):
        return None
    f = [x.strip() for x in j["fields"]]
    ix = {k: f.index(k) for k in ("證券代號", "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數",
                                   "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")}
    out = []
    for r in j["data"]:
        code = r[ix["證券代號"]].strip()
        if not CODE_OK.match(code):
            continue
        out.append({"date": d, "code": code, "market": "TWSE", "unit": "shares",
                    "foreign_net": i(r[ix["外陸資買賣超股數(不含外資自營商)"]]),
                    "foreign_dealer_net": i(r[ix["外資自營商買賣超股數"]]),
                    "trust_net": i(r[ix["投信買賣超股數"]]), "dealer_net": i(r[ix["自營商買賣超股數"]]),
                    "total_net": i(r[ix["三大法人買賣超股數"]])})
    return out


def twse_market(s, d, sleep):
    j = get(s, "https://www.twse.com.tw/rwd/zh/fund/BFI82U",
            {"type": "day", "dayDate": d.replace("-", ""), "response": "json"}, sleep=sleep).json()
    if j.get("stat") != "OK" or str(j.get("date")) != d.replace("-", ""):
        return None
    v = {r[0].strip(): i(r[3]) for r in j["data"]}
    return {"date": d, "code": "_MARKET", "market": "TWSE", "unit": "TWD",
            "foreign_net": v["外資及陸資(不含外資自營商)"], "foreign_dealer_net": v["外資自營商"], "trust_net": v["投信"],
            "dealer_net": v["自營商(自行買賣)"] + v["自營商(避險)"], "total_net": v["合計"]}


def tpex_stock(s, d, sleep):
    j = get(s, "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
            {"type": "Daily", "sect": "EW", "date": d.replace("-", "/"), "response": "json"}, sleep=sleep).json()
    t = next((t for t in j.get("tables", []) if t.get("data")), None)
    if str(j.get("stat", "")).lower() != "ok" or not t or roc_to_iso(t["date"]) != d:
        return None
    if len(t["fields"]) != 24:
        raise ValueError(f"櫃買法人表欄位數改變：{len(t['fields'])}")
    out = []
    for r in t["data"]:
        code = str(r[0]).strip()
        if not CODE_OK.match(code):
            continue
        n = [i(x) or 0 for x in r[2:24]]
        # 索引（相對 r[2]）：買賣超 = 2 外資不含自營、5 外資自營、8 外資合計、11 投信、14 自營自行、17 避險、20 自營合計、21 三大法人
        if n[8] != n[2] + n[5] or n[20] != n[14] + n[17] or n[21] != n[8] + n[11] + n[20]:
            raise ValueError(f"櫃買法人表加總檢查不符（{code}），欄位順序可能已改變")
        out.append({"date": d, "code": code, "market": "TPEX", "unit": "shares", "foreign_net": n[2],
                    "foreign_dealer_net": n[5], "trust_net": n[11], "dealer_net": n[20], "total_net": n[21]})
    return out


def tpex_market(s, d, sleep):
    j = get(s, "https://www.tpex.org.tw/www/zh-tw/insti/summary",
            {"type": "Daily", "date": d.replace("-", "/"), "response": "json"}, sleep=sleep).json()
    t = next((t for t in j.get("tables", []) if t.get("data")), None)
    if not t or roc_to_iso(t["date"]) != d:
        return None
    v = {r[0].replace("　", "").strip(): i(r[3]) for r in t["data"]}
    return {"date": d, "code": "_MARKET", "market": "TPEX", "unit": "TWD",
            "foreign_net": v["外資及陸資(不含自營商)"], "foreign_dealer_net": v["外資自營商"], "trust_net": v["投信"],
            "dealer_net": v["自營商合計"], "total_net": v["三大法人合計*"]}


def trading_dates():
    """prices.csv 中的台股交易日（0050 有資料的日子），由舊到新。"""
    ds = set()
    with open(path("data", "prices.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["code"] == "0050":
                ds.add(r["date"])
    return sorted(ds)


def add_derived(rows, closes):
    """張數與以收盤價換算的近似金額（見 FIELDS 說明）。"""
    for r in rows:
        if r["code"] == "_MARKET":
            continue
        cl = num(closes.get((r["code"], r["date"])))
        r["close"] = "" if cl is None else cl
        for k in ("foreign", "trust", "dealer", "total"):
            v = num(r.get(f"{k}_net"))
            if k == "foreign" and v is not None:
                v += num(r.get("foreign_dealer_net")) or 0   # 張數與金額的「外資」= 外資不含自營 + 外資自營
            r[f"{k}_net_lots"] = "" if v is None else round(v / 1000, 3)
            r[f"{k}_net_value_est"] = "" if v is None or cl is None else round(v * cl)


def main():
    cfg = load_config()
    c = cfg["institutional"]
    sleep = float(cfg["data"]["request_sleep_sec"])
    n = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else int(c["refetch_recent_days"])
    tds = trading_dates()
    targets = tds[-n:]
    recent = set(tds[-int(c["refetch_recent_days"]):])
    # 近期檔 data/institutional.csv（個股 recent_trading_days 天、全市場合計 keep_trading_days_market 天）
    # + 分月歷史 data/history/institutional/YYYY-MM.csv（keep_trading_days_stock 天）
    store = DailyStore("institutional", FIELDS, ("date", "market", "code"), int(c["recent_trading_days"]),
                       int(c["keep_trading_days_stock"]), archive=bool(c["archive_history"]),
                       long_codes=("_MARKET",), long_days=int(c["keep_trading_days_market"]))
    store.load_months(targets)
    closes = {}
    tset = set(targets)
    with open(path("data", "prices.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["date"] in tset:
                closes[(r["code"], r["date"])] = r["close"]
    s = requests.Session()
    status = {}
    blocked = None
    for d in targets:
        if blocked:
            break
        st = {}
        for market, fs, fm in (("TWSE", twse_stock, twse_market), ("TPEX", tpex_stock, tpex_market)):
            if store.has(d, market) and d not in recent:
                st[market] = "cached"
                continue
            try:
                rows = fs(s, d, sleep)
                mrow = fm(s, d, sleep)
                if rows is None or mrow is None:
                    st[market] = "pending（交易所尚未公布）"
                    continue
                add_derived(rows, closes)
                store.replace(d, market, rows + [mrow])
                st[market] = f"ok:{len(rows)}"
            except Blocked as e:   # 被擋：不重試、不再送請求，存檔後以代碼 2 結束
                st[market] = f"blocked: {e}"
                blocked = str(e)
                break
            except Exception as e:  # noqa: BLE001
                st[market] = f"error: {type(e).__name__}: {e}"
        status[d] = st
        log(f"{d} {st}")
    n_recent = store.save()
    latest = {m: max((k[0] for k, r in store.rows.items() if r.get("market") == m and r["code"] != "_MARKET"),
                     default=None) for m in ("TWSE", "TPEX")}
    write_json(path("data", "institutional_status.json"),
               {"generated_at": now_tw().isoformat(timespec="seconds"), "latest_date": latest,
                "expected_latest_trading_date": tds[-1] if tds else None,
                "recent_file_days": n_recent, "by_date": status, "blocked": blocked})
    log(f"寫入 {OUT}；最新 {latest}")
    if blocked:
        log(f"被擋，停止（不重試）：{blocked}")
        sys.exit(2)

if __name__ == "__main__":
    main()
