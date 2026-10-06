
#!/usr/bin/env python3
"""每日抓取台股「上市 + 上櫃全部股票」的日K資料，存成 data/prices.csv 與 data/meta.json。

台股主要來源：證交所 MI_INDEX 與櫃買中心 daily_close_quotes 的「單日全市場」資料，
  按日期抓取（每個交易日 2 次請求），第一次執行會回補約 170 個日曆天（>= 100 個交易日），
  之後只補缺的日期，並固定重抓最近 2 個平日避免早盤跑到不完整資料。
備援：關注名單（WATCHLIST）內的個股若全市場抓取缺資料，改用逐檔的官方月資料，再不行用 FinMind。
韓股（三星、海力士）、匯率與美股用 yfinance，失敗不影響台股。

歷史資料會累積在 data/prices.csv，重複的日期以新資料覆蓋。
注意：上櫃（TPEX）全市場資料的成交股數以千股為單位、且比逐檔舊資料低約 6~15%（疑似不含零股／盤後定價，
未確認原因），開高低收不受影響；同一檔在回補期間內口徑一致，量比（volRatio）仍可比較。
meta.json 的 latest_date / sources 只列 WATCHLIST 與 GLOBAL 的代號，避免檔案過大。
"""
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests
 
TZ_TW = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
PRICES_CSV = os.path.join(DATA_DIR, "prices.csv")
META_JSON = os.path.join(DATA_DIR, "meta.json")
 
# code -> (名稱, 市場)  市場: TWSE 上市 / TPEX 上櫃
WATCHLIST = {
    "2408": ("南亞科", "TWSE"),
    "3017": ("奇鋐", "TWSE"),
    "3324": ("雙鴻", "TPEX"),
    "3653": ("健策", "TWSE"),
    "6196": ("帆宣", "TWSE"),
    "00735": ("國泰臺韓科技", "TWSE"),
    "3081": ("聯亞", "TPEX"),
    # ---- 發財儀表板持股（台股）----
    "0050": ("元大台灣50", "TWSE"),
    "00685L": ("群益臺灣加權正2", "TWSE"),
    "2308": ("台達電", "TWSE"),
    "2327": ("國巨", "TWSE"),
    "2330": ("台積電", "TWSE"),
    "2357": ("華碩", "TWSE"),
    "2454": ("聯發科", "TWSE"),
    "3042": ("晶技", "TWSE"),
    "3189": ("景碩", "TWSE"),
    "3711": ("日月光投控", "TWSE"),
    "6223": ("旺矽", "TPEX"),
}
# 韓股與匯率：代碼 -> 名稱（yfinance 代號）
GLOBAL = {
    "005930.KS": "三星電子",
    "000660.KS": "SK海力士",
    "TWDKRW=X": "台幣兌韓元",
    # ---- 發財儀表板持股（美股）與匯率 ----
    "USDTWD=X": "美元兌台幣",
    "GOOG": "Alphabet C",
    "MS": "Morgan Stanley",
    "QQQM": "Invesco NASDAQ 100 ETF",
    "SOXX": "iShares Semiconductor ETF",
    "VOO": "Vanguard S&P 500 ETF",
    # ---- 盯盤名單（美股）----
    "NVDA": "NVIDIA",
}
 
MONTHS_BACK = 6          # 備援（逐檔月資料）每次回抓幾個月，確保能算 60 日線
KEEP_DAYS = 200          # csv 保留的日曆天數
SLEEP = 3.0              # 官方站台有頻率限制，每次請求間隔秒數
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; watchlist-bot/1.0)"}
FIELDS = ["code", "date", "open", "high", "low", "close", "volume"]

BACKFILL_DAYS = 170      # 全市場回補的日曆天數（約 115 個交易日，確保 >= 100 個交易日）
REFETCH_RECENT = 2       # 最近幾個平日一律重抓（避免盤後資料尚未完整時抓到半套）
COMPLETE_MIN_ROWS = 1800  # 某日台股筆數達此值視為已完整（上市約 1300 + 上櫃約 950，單一市場不會超過）
TWSE_ALL_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
# 櫃買新版網址，會依 date 回傳該日資料；type=EW 為上櫃股票（不含權證）。
# 注意：舊的 daily_close_quotes/stk_quote_result.php 會忽略日期、永遠回最新一天，不能用來回補。
TPEX_ALL_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
# 只收一般股票、特別股（如 2882A）與 ETF（如 0050、00685L、00400A、006201）；
# 權證、牛熊證、ETN、受益證券等不收。
CODE_OK = re.compile(r"^(\d{4}[A-Z]?|00\d{2,4}[A-Z]?)$")
 
 
def num(s):
    """'3,555.00' -> 3555.0；無法解析回傳 None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "X0.00"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
 
 
def roc_to_iso(s):
    """'115/09/24' -> '2026-09-24'"""
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"
 
 
def month_starts(n):
    today = datetime.now(TZ_TW).date().replace(day=1)
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out
 
 
# ---------- 來源 1：證交所 ----------
def fetch_twse(code):
    rows = []
    for y, m in month_starts(MONTHS_BACK):
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?date={y}{m:02d}01&stockNo={code}&response=json"
        )
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("stat") != "OK":
            continue
        for d in j.get("data", []):
            # 日期,成交股數,成交金額,開,高,低,收,漲跌,筆數
            close = num(d[6])
            if close is None:
                continue
            rows.append(
                dict(code=code, date=roc_to_iso(d[0]), open=num(d[3]), high=num(d[4]),
                     low=num(d[5]), close=close, volume=num(d[1]))
            )
        time.sleep(SLEEP)
    return rows
 
 
# ---------- 來源 1：櫃買中心 ----------
def fetch_tpex(code):
    rows = []
    for y, m in month_starts(MONTHS_BACK):
        roc = f"{y - 1911}/{m:02d}"
        url = (
            "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/"
            f"st43_result.php?l=zh-tw&d={roc}&stkno={code}"
        )
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        j = r.json()
        for d in j.get("aaData", []) or j.get("data", []) or []:
            # 日期,成交仟股,成交仟元,開,高,低,收,漲跌,筆數
            close = num(d[6])
            if close is None:
                continue
            vol = num(d[1])
            rows.append(
                dict(code=code, date=roc_to_iso(d[0].replace("*", "")), open=num(d[3]),
                     high=num(d[4]), low=num(d[5]), close=close,
                     volume=vol * 1000 if vol is not None else None)
            )
        time.sleep(SLEEP)
    return rows
 
 
# ---------- 來源 2：FinMind（上市上櫃皆可） ----------
def fetch_finmind(code):
    start = (datetime.now(TZ_TW).date() - timedelta(days=MONTHS_BACK * 31)).isoformat()
    r = requests.get(
        "https://api.finmindtrade.com/api/v4/data",
        params={"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    rows = []
    for d in j.get("data", []):
        close = d.get("close")
        if not close:
            continue
        rows.append(
            dict(code=code, date=d["date"], open=d.get("open"), high=d.get("max"),
                 low=d.get("min"), close=close, volume=d.get("Trading_Volume"))
        )
    return rows
 
 
def fetch_tw(code, market):
    errors = []
    primary = fetch_twse if market == "TWSE" else fetch_tpex
    for name, fn in ((f"official-{market}", primary), ("finmind", fetch_finmind)):
        try:
            rows = fn(code)
            if rows:
                return rows, name, errors
            errors.append(f"{name}: 沒有資料")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return [], None, errors
 
 
# ---------- 全市場：按日期抓上市 / 上櫃全部股票 ----------
def _norm(fields):
    return [str(f).strip() for f in fields]


def _col(fields, *names):
    fields = _norm(fields)
    for n in names:
        if n in fields:
            return fields.index(n)
    raise KeyError(f"找不到欄位 {names}，實際欄位：{fields}")


def _rows_from_table(fields, data, date_iso, c_code, c_open, c_high, c_low, c_close, c_vol):
    """把一張全市場表轉成 row 清單；沒成交（價格 '--'）或不收的代號會略過。"""
    i_code, i_o, i_h, i_l, i_c, i_v = (_col(fields, *c) for c in (c_code, c_open, c_high, c_low, c_close, c_vol))
    rows = []
    for d in data:
        code = str(d[i_code]).strip()
        if not CODE_OK.match(code):
            continue
        o, h, l, c, v = num(d[i_o]), num(d[i_h]), num(d[i_l]), num(d[i_c]), num(d[i_v])
        if None in (o, h, l, c):
            continue
        rows.append(dict(code=code, date=date_iso, open=o, high=h, low=l, close=c, volume=v))
    return rows


def parse_twse_day(j, want_date):
    """證交所 MI_INDEX JSON -> row 清單；沒有資料（休市或尚未公布）回傳 None。"""
    if j.get("stat") != "OK":
        return None
    t = next((t for t in j.get("tables", [])
              if "證券代號" in _norm(t.get("fields", [])) and "收盤價" in _norm(t.get("fields", []))), None)
    if not t or not t.get("data"):
        return None
    date_iso = datetime.strptime(str(j.get("date")), "%Y%m%d").date().isoformat()
    if date_iso != want_date.isoformat():
        return None
    return _rows_from_table(t["fields"], t["data"], date_iso, ("證券代號",), ("開盤價",), ("最高價",),
                            ("最低價",), ("收盤價",), ("成交股數",))


def parse_tpex_day(j, want_date):
    """櫃買 afterTrading/otc JSON -> row 清單；沒有資料（休市或尚未公布）回傳 None。"""
    if str(j.get("stat", "")).lower() != "ok":
        return None
    t = next((t for t in j.get("tables", [])
              if "代號" in _norm(t.get("fields", [])) and "收盤" in _norm(t.get("fields", [])) and t.get("data")), None)
    if not t:
        return None
    date_iso = roc_to_iso(t["date"]) if t.get("date") else datetime.strptime(str(j.get("date")), "%Y%m%d").date().isoformat()
    if date_iso != want_date.isoformat():
        return None
    return _rows_from_table(t["fields"], t["data"], date_iso, ("代號",), ("開盤",), ("最高",),
                            ("最低",), ("收盤",), ("成交股數",))


def _get_json(session, url, params, tries=3):
    last = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(SLEEP * (i + 2))
    raise last


def fetch_bulk_day(session, d):
    """抓某一天的上市與上櫃全部股票。回傳 {市場: (rows 或 None, 錯誤訊息或 None)}。
    rows 為 None 且沒有錯誤 = 該市場當天沒有資料（休市或尚未公布）。"""
    out = {}
    try:
        j = _get_json(session, TWSE_ALL_URL, {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
        out["TWSE"] = (parse_twse_day(j, d), None)
    except Exception as e:  # noqa: BLE001
        out["TWSE"] = (None, f"{type(e).__name__}: {e}")
    time.sleep(SLEEP)
    try:
        j = _get_json(session, TPEX_ALL_URL, {"date": d.strftime("%Y/%m/%d"), "type": "EW", "id": "", "response": "json"})
        out["TPEX"] = (parse_tpex_day(j, d), None)
    except Exception as e:  # noqa: BLE001
        out["TPEX"] = (None, f"{type(e).__name__}: {e}")
    time.sleep(SLEEP)
    return out


def weekdays_back(days, today):
    d, out = today - timedelta(days=days), []
    while d <= today:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------- 韓股與匯率 ----------
def fetch_global():
    out, status = [], {}
    try:
        import yfinance as yf
    except Exception as e:  # noqa: BLE001
        return out, {k: f"yfinance 未安裝: {e}" for k in GLOBAL}
    for sym in GLOBAL:
        try:
            df = yf.Ticker(sym).history(period="6mo", interval="1d", auto_adjust=False)
            n = 0
            for idx, row in df.iterrows():
                if row["Close"] != row["Close"]:  # NaN
                    continue
                out.append(
                    dict(code=sym, date=idx.strftime("%Y-%m-%d"), open=float(row["Open"]),
                         high=float(row["High"]), low=float(row["Low"]),
                         close=float(row["Close"]), volume=float(row["Volume"]))
                )
                n += 1
            status[sym] = "ok" if n else "沒有資料"
        except Exception as e:  # noqa: BLE001
            status[sym] = f"{type(e).__name__}: {e}"
    return out, status
 
 
# ---------- 讀寫 csv ----------
def load_existing():
    """(code, date) -> [open, high, low, close, volume]（字串或數字）"""
    data = {}
    if os.path.exists(PRICES_CSV):
        with open(PRICES_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                data[(r["code"], r["date"])] = [r["open"], r["high"], r["low"], r["close"], r["volume"]]
    return data


def put(merged, r):
    merged[(r["code"], r["date"])] = [("" if r[k] is None else r[k]) for k in FIELDS[2:]]


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    merged = load_existing()
    now = datetime.now(TZ_TW)
    today = now.date()
    meta = {"updated_at": now.isoformat(timespec="seconds"), "sources": {}, "errors": {}}
    ok_any = False

    # ---- 1. 全市場（按日期）：上市 + 上櫃全部股票 ----
    days = weekdays_back(BACKFILL_DAYS, today)
    tw_count = Counter(d for (c, d) in merged if c not in GLOBAL)
    targets = sorted(
        {d for d in days if tw_count.get(d.isoformat(), 0) < COMPLETE_MIN_ROWS} | set(days[-REFETCH_RECENT:])
    )
    recent5 = set(days[-5:])
    market_err = {"TWSE": False, "TPEX": False}
    bulk = {"dates_requested": len(targets), "dates_with_data": 0, "rows": 0, "errors": {}}
    print(f"全市場：需要抓 {len(targets)} 個平日", file=sys.stderr)
    session = requests.Session()
    for i, d in enumerate(targets, 1):
        res = fetch_bulk_day(session, d)
        got = 0
        for market, (rows, err) in res.items():
            if err:
                bulk["errors"].setdefault(d.isoformat(), []).append(f"{market}: {err}")
                if d in recent5:
                    market_err[market] = True
            for r in rows or []:
                put(merged, r)
                got += 1
        if got:
            bulk["dates_with_data"] += 1
            bulk["rows"] += got
            ok_any = True
        flag = " (有錯誤)" if d.isoformat() in bulk["errors"] else ""
        print(f"[{i}/{len(targets)}] {d} {got} 筆{flag}", file=sys.stderr)
    # 錯誤紀錄只留最近 10 天，避免 meta 過長
    bulk["errors"] = dict(sorted(bulk["errors"].items())[-10:])
    meta["bulk"] = bulk

    # ---- 2. 備援：關注名單個股若全市場缺資料，改走逐檔來源（官方月資料 → FinMind）----
    latest_by_code = {}
    for (c, d) in merged:
        if d > latest_by_code.get(c, ""):
            latest_by_code[c] = d
    latest_tw = max((d for c, d in latest_by_code.items() if c not in GLOBAL), default="")
    for code, (name, market) in WATCHLIST.items():
        if latest_by_code.get(code, "") < latest_tw or market_err[market]:
            rows, src, errs = fetch_tw(code, market)
            meta["sources"][code] = src
            if errs:
                meta["errors"][code] = errs
            print(f"{code} {name}: 備援 {len(rows)} 筆, 來源={src}", file=sys.stderr)
            for r in rows:
                put(merged, r)
            ok_any = ok_any or bool(rows)
            time.sleep(SLEEP)
        else:
            meta["sources"][code] = f"bulk-{market}"

    g_rows, g_status = fetch_global()
    meta["global_status"] = g_status
    for r in g_rows:
        put(merged, r)

    if not ok_any:
        print("所有台股來源都失敗，保留舊資料不覆寫。", file=sys.stderr)
        with open(META_JSON, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        sys.exit(1)

    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    keys = sorted(k for k in merged if k[1] >= cutoff)
    with open(PRICES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for (c, d) in keys:
            w.writerow([c, d, *merged[(c, d)]])

    # 每檔最新日期（只列關注名單與海外代號），方便快速確認資料是否到位
    latest = {}
    for (c, d) in keys:
        if (c in WATCHLIST or c in GLOBAL) and d > latest.get(c, ""):
            latest[c] = d
    meta["latest_date"] = latest
    tw_latest = max((d for (c, d) in keys if c not in GLOBAL), default=None)
    meta["tw_latest_date"] = tw_latest
    meta["tw_codes_on_latest_date"] = sum(1 for (c, d) in keys if d == tw_latest and c not in GLOBAL)
    with open(META_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
