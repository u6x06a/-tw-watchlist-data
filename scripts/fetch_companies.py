#!/usr/bin/env python3
"""上市、上櫃證券基本資料 -> data/companies.csv（每週更新；config companies.refresh_days）。

欄位：code,name,market,sec_type,industry_code,industry,full_delivery,shares_outstanding,limit_pct,listed_date,data_date,source
  sec_type        stock 普通股（含 KY）/ preferred 特別股 / etf / etf_leveraged / etf_inverse / etf_bond / other
  industry        產業別名稱。基本資料只有代碼，名稱由同公司在月營收表的「產業別」對照（同市場多數決）
  full_delivery   是否為變更交易（全額交割）：Y / N；來源抓取失敗時留空（= 查不到，不猜）
  shares_outstanding  已發行普通股數（上市櫃公司基本資料）；上櫃 ETF 取櫃買日行情「發行股數」；查不到留空
  limit_pct       漲跌幅限制：10 / 20 / none（無限制）/ 空白（查不到）。股票一律 10；
                  ETF 由證交所 TWT84U「今日漲停價 / 開盤參考價」、櫃買日行情「次日漲停價 / 收盤」反推後分級
                  （實測：台灣加權正2 為 20、國外成分 ETF 漲停價 9999.95 = none）
  data_date       該列資料的出表日期（基本資料）或行情日期（ETF / 漲跌幅）
來源（皆已實測，2026-10-16）：
  https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv / t187ap03_O.csv   上市 / 上櫃公司基本資料
  https://mopsfin.twse.com.tw/opendata/t187ap05_L.csv / t187ap05_O.csv   上市 / 上櫃月營收（取產業別名稱）
  https://openapi.twse.com.tw/v1/exchangeReport/TWT84U                   上市個股股價升降幅度（含 ETF 名稱）
  https://openapi.twse.com.tw/v1/exchangeReport/TWT85U                   集中市場證券變更交易
  https://www.tpex.org.tw/openapi/v1/tpex_cmode                          上櫃變更交易、分盤、管理股票
  https://www.tpex.org.tw/www/zh-tw/afterTrading/otc                      上櫃日行情（名稱、發行股數、次日漲停價）

用法：python scripts/fetch_companies.py [--force]
"""
import csv
import io
import os
import sys
from collections import Counter
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE_OK, Blocked, get, load_config, log, now_tw, num, path, roc_to_iso, sec_type_from_code  # noqa: E402

OUT = path("data", "companies.csv")
FIELDS = ["code", "name", "market", "sec_type", "industry_code", "industry", "full_delivery",
          "shares_outstanding", "limit_pct", "listed_date", "data_date", "source"]
MOPS = "https://mopsfin.twse.com.tw/opendata/"


def read_csv_url(s, url, sleep):
    raw = g(s, url, sleep=sleep).content
    for enc in ("utf-8-sig", "cp950"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"無法解碼 {url}")


_BLOCKED = []


def g(*a, **k):
    """get() 的包裝：一旦被擋（403/429/HTML），之後的請求一律不送出，直接丟出 Blocked。"""
    if _BLOCKED:
        raise Blocked(_BLOCKED[0])
    try:
        return get(*a, **k)
    except Blocked as e:
        _BLOCKED.append(str(e))
        raise


def limit_from(up, ref):
    """漲停價 / 參考價 反推的漲跌幅級距：10、20 或 'none'（無限制）。跳動單位會讓實際比例略低於級距
    （例如 9.6%），所以分級而不是直接四捨五入。新上市首五日的超大值也歸 'none'，股票類別之後一律覆寫為 10。"""
    if up is None or not ref:
        return None
    if up >= 9999:
        return "none"
    pct = (up / ref - 1) * 100
    if pct < 15:
        return 10
    if pct < 30:
        return 20
    return "none"


def latest_trading_date():
    """prices.csv 最新的台股日期（避免多抓一次）；沒有就用今天。"""
    p = path("data", "meta.json")
    try:
        import json
        return json.load(open(p, encoding="utf-8")).get("tw_latest_date")
    except Exception:  # noqa: BLE001
        return None


def main():
    cfg = load_config()
    sleep = float(cfg["data"]["request_sleep_sec"])
    if "--force" not in sys.argv and os.path.exists(OUT):
        age = datetime.now().timestamp() - os.path.getmtime(OUT)
        with open(OUT, encoding="utf-8") as f:
            has_rows = sum(1 for _ in f) > 1
        if has_rows and age < cfg["companies"]["refresh_days"] * 86400:
            log(f"companies.csv 未滿 {cfg['companies']['refresh_days']} 天，略過（--force 可強制）")
            return
    s = requests.Session()
    rows, status = {}, {}

    # 1. 上市 / 上櫃公司基本資料（普通股）
    ind_code = {}
    for mk, suf in (("TWSE", "L"), ("TPEX", "O")):
        try:
            for r in read_csv_url(s, MOPS + f"t187ap03_{suf}.csv", sleep):
                code = r["公司代號"].strip()
                if not CODE_OK.match(code):
                    continue
                listed = r.get("上市日期") or r.get("上櫃日期") or ""
                rows[code] = {"code": code, "name": r["公司簡稱"].strip(), "market": mk, "sec_type": "stock",
                              "industry_code": r["產業別"].strip(),
                              "shares_outstanding": int(num(r["已發行普通股數或TDR原股發行股數"]) or 0) or "",
                              "listed_date": f"{listed[:4]}-{listed[4:6]}-{listed[6:8]}" if len(listed) == 8 else "",
                              "data_date": roc_to_iso(r["出表日期"]), "source": f"t187ap03_{suf}"}
                ind_code[code] = (mk, r["產業別"].strip())
            status[f"t187ap03_{suf}"] = "ok"
        except Exception as e:  # noqa: BLE001
            status[f"t187ap03_{suf}"] = f"{type(e).__name__}: {e}"

    # 2. 產業別代碼 -> 名稱（同市場多數決，月營收表有名稱）
    votes = {}
    for mk, suf in (("TWSE", "L"), ("TPEX", "O")):
        try:
            for r in read_csv_url(s, MOPS + f"t187ap05_{suf}.csv", sleep):
                code = r["公司代號"].strip()
                if code in ind_code and ind_code[code][0] == mk:
                    votes.setdefault((mk, ind_code[code][1]), Counter())[r["產業別"].strip()] += 1
            status[f"t187ap05_{suf}"] = "ok"
        except Exception as e:  # noqa: BLE001
            status[f"t187ap05_{suf}"] = f"{type(e).__name__}: {e}"
    ind_name = {k: c.most_common(1)[0][0] for k, c in votes.items()}
    conflicts = {f"{k[0]}-{k[1]}": dict(c) for k, c in votes.items() if len(c) > 1}
    for r in rows.values():
        r["industry"] = ind_name.get((r["market"], r["industry_code"]), "")

    # 3. 上市：TWT84U（全部證券含 ETF 的名稱與漲跌停價）
    try:
        j = g(s, "https://openapi.twse.com.tw/v1/exchangeReport/TWT84U", sleep=sleep).json()
        d84 = now_tw().date().isoformat()
        for x in j:
            code = x["Code"].strip()
            if not CODE_OK.match(code):
                continue
            lim = limit_from(num(x.get("TodayLimitUp")), num(x.get("TodayOpeningRefPrice")))
            r = rows.get(code)
            if r is None:
                r = rows[code] = {"code": code, "name": x["Name"].strip(), "market": "TWSE",
                                  "sec_type": sec_type_from_code(code), "data_date": d84, "source": "TWT84U"}
            elif r["market"] != "TWSE":
                continue
            if lim is not None:
                r["limit_pct"] = lim
        status["TWT84U"] = f"ok（{d84} 當日）"
    except Exception as e:  # noqa: BLE001
        status["TWT84U"] = f"{type(e).__name__}: {e}"

    # 4. 上櫃：最新交易日的日行情（名稱、發行股數、次日漲停價）
    tdate = latest_trading_date()
    if tdate:
        try:
            j = g(s, "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                    {"date": tdate.replace("-", "/"), "type": "EW", "id": "", "response": "json"}, sleep=sleep).json()
            t = next(t for t in j.get("tables", []) if t.get("data"))
            fs = [f.strip() for f in t["fields"]]
            ic, iname, iclose, ishares, iup = (fs.index(k) for k in ("代號", "名稱", "收盤", "發行股數", "次日漲停價"))
            tday = roc_to_iso(t["date"])
            for d in t["data"]:
                code = str(d[ic]).strip()
                if not CODE_OK.match(code):
                    continue
                r = rows.get(code)
                if r is None:
                    r = rows[code] = {"code": code, "name": str(d[iname]).strip(), "market": "TPEX",
                                      "sec_type": sec_type_from_code(code), "data_date": tday, "source": "tpex_otc"}
                    sh = num(d[ishares])
                    r["shares_outstanding"] = int(sh) if sh else ""
                elif r["market"] != "TPEX":
                    continue
                lim = limit_from(num(d[iup]), num(d[iclose]))
                if lim is not None:
                    r["limit_pct"] = lim
            status["tpex_otc"] = f"ok（{tday}）"
        except Exception as e:  # noqa: BLE001
            status["tpex_otc"] = f"{type(e).__name__}: {e}"

    # 5. 全額交割（變更交易）
    full = {"TWSE": None, "TPEX": None}
    try:
        j = g(s, "https://openapi.twse.com.tw/v1/exchangeReport/TWT85U", sleep=sleep).json()
        full["TWSE"] = {x["Code"].strip() for x in j}
        status["TWT85U"] = f"ok（{len(full['TWSE'])} 檔）"
    except Exception as e:  # noqa: BLE001
        status["TWT85U"] = f"{type(e).__name__}: {e}"
    try:
        j = g(s, "https://www.tpex.org.tw/openapi/v1/tpex_cmode", sleep=sleep).json()
        full["TPEX"] = {x["SecuritiesCompanyCode"].strip() for x in j
                        if str(x.get("AlteredTrading", "")).strip() in ("Y", "Ｙ")}
        status["tpex_cmode"] = f"ok（變更交易 {len(full['TPEX'])} 檔）"
    except Exception as e:  # noqa: BLE001
        status["tpex_cmode"] = f"{type(e).__name__}: {e}"
    for r in rows.values():
        f = full.get(r["market"])
        r["full_delivery"] = "" if f is None else ("Y" if r["code"] in f else "N")
        # 股票（普通股、特別股）漲跌幅一律 10%；新上市首五日無限制由 adjust.py 另外判斷
        if r["sec_type"] in ("stock", "preferred"):
            r["limit_pct"] = 10

    if _BLOCKED:
        log(f"被擋，停止且保留舊檔（不重試）：{_BLOCKED[0]}；{status}")
        sys.exit(2)
    if not rows:
        log(f"所有來源失敗，保留舊檔：{status}")
        sys.exit(1)
    tmp = OUT + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for code in sorted(rows):
            w.writerow({k: rows[code].get(k, "") for k in FIELDS})
    os.replace(tmp, OUT)
    import json
    from common import write_json
    write_json(path("data", "companies_status.json"),
               {"generated_at": now_tw().isoformat(timespec="seconds"), "rows": len(rows),
                "by_type": dict(Counter(r["sec_type"] for r in rows.values())),
                "sources": status, "industry_name_conflicts": conflicts,
                "industry_unmapped": sorted({f"{r['market']}-{r['industry_code']}" for r in rows.values()
                                             if r.get("industry_code") and not r.get("industry")})})
    log(f"寫入 {OUT}：{len(rows)} 筆；來源 {json.dumps(status, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
