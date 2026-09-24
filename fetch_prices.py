
#!/usr/bin/env python3
"""每日抓取關注名單的日K資料，存成 data/prices.csv 與 data/meta.json。
 
資料來源（依序嘗試，任一成功即可）：
  1. 證交所 / 櫃買中心官方開放資料
  2. FinMind 公開 API（備援，涵蓋上市與上櫃）
韓股（三星、海力士）與韓元匯率用 yfinance，失敗不影響台股。
 
歷史資料會累積在 data/prices.csv，重複的日期以新資料覆蓋。
"""
import csv
import json
import os
import sys
import time
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
}
# 韓股與匯率：代碼 -> 名稱（yfinance 代號）
GLOBAL = {
    "005930.KS": "三星電子",
    "000660.KS": "SK海力士",
    "TWDKRW=X": "台幣兌韓元",
}
 
MONTHS_BACK = 6          # 每次回抓幾個月，確保能算 60 日線
KEEP_DAYS = 200          # csv 保留的日曆天數
SLEEP = 3.0              # 官方站台有頻率限制，每次請求間隔秒數
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; watchlist-bot/1.0)"}
FIELDS = ["code", "date", "open", "high", "low", "close", "volume"]
 
 
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
    data = {}
    if os.path.exists(PRICES_CSV):
        with open(PRICES_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                data[(r["code"], r["date"])] = r
    return data
 
 
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    merged = load_existing()
    meta = {"updated_at": datetime.now(TZ_TW).isoformat(timespec="seconds"), "sources": {}, "errors": {}}
    ok_any = False
 
    for code, (name, market) in WATCHLIST.items():
        rows, src, errs = fetch_tw(code, market)
        meta["sources"][code] = src
        if errs:
            meta["errors"][code] = errs
        print(f"{code} {name}: {len(rows)} 筆, 來源={src}", file=sys.stderr)
        for r in rows:
            merged[(r["code"], r["date"])] = {k: ("" if r[k] is None else r[k]) for k in FIELDS}
        ok_any = ok_any or bool(rows)
        time.sleep(SLEEP)
 
    g_rows, g_status = fetch_global()
    meta["global_status"] = g_status
    for r in g_rows:
        merged[(r["code"], r["date"])] = {k: ("" if r[k] is None else r[k]) for k in FIELDS}
 
    if not ok_any:
        print("所有台股來源都失敗，保留舊資料不覆寫。", file=sys.stderr)
        with open(META_JSON, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        sys.exit(1)
 
    cutoff = (datetime.now(TZ_TW).date() - timedelta(days=KEEP_DAYS)).isoformat()
    rows = sorted((r for (c, d), r in merged.items() if d >= cutoff), key=lambda r: (r["code"], r["date"]))
    with open(PRICES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
 
    # 每檔最新日期，方便我快速確認資料是否到位
    latest = {}
    for r in rows:
        latest[r["code"]] = max(latest.get(r["code"], ""), r["date"])
    meta["latest_date"] = latest
    with open(META_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False, indent=2), file=sys.stderr)
 
 
if __name__ == "__main__":
    main()
 
