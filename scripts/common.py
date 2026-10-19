"""各腳本共用：路徑、設定檔、HTTP、數字解析、代號分類。"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config", "rules.yaml")
TZ_TW = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; watchlist-bot/1.0)"}

# 只收一般股票、特別股（2882A）與 ETF（0050、00685L、006201）；權證、ETN 等不收（與 fetch_prices.py 相同）
CODE_OK = re.compile(r"^(\d{4}[A-Z]?|00\d{2,4}[A-Z]?)$")


def path(*p):
    return os.path.join(ROOT, *p)


def now_tw():
    return datetime.now(TZ_TW)


def load_config():
    import yaml
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def num(s):
    """'3,555.00' -> 3555.0；'--'、空白、文字回傳 None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "---", "X0.00"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    """'115/09/24'、'115年09月24日'、'1150924' -> '2026-09-24'"""
    s = str(s).strip()
    if re.fullmatch(r"\d{7}", s):
        y, m, d = int(s[:3]), int(s[3:5]), int(s[5:])
    else:
        y, m, d = map(int, re.findall(r"\d+", s)[:3])
    return f"{y + 1911:04d}-{m:02d}-{d:02d}"


class Blocked(Exception):
    """被來源站台擋下（HTTP 403/429，或該回 JSON 卻回 HTML 頁面）。不重試；呼叫端應存檔後停止，不再送出請求。"""


def get(session, url, params=None, sleep=3.0, tries=3, timeout=60, expect_json=None):
    """GET 並在成功後固定間隔 sleep 秒（官方站台頻率限制）。
    403/429 或（expect_json 且回 HTML）→ 立刻丟出 Blocked，不重試；其他錯誤重試，最後一次仍失敗則丟出例外。
    expect_json 預設：網址含 response=json 或 /openapi/ 時為 True。"""
    if expect_json is None:
        expect_json = (params or {}).get("response") == "json" or "/openapi/" in url
    last = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code in (403, 429):
                raise Blocked(f"HTTP {r.status_code} {url}")
            r.raise_for_status()
            if expect_json and r.text.lstrip()[:1] == "<":
                raise Blocked(f"回應是 HTML 而不是 JSON（疑似被擋）{url}")
            time.sleep(sleep)
            return r
        except Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(sleep * (i + 2))
    raise last


def is_global(code):
    """yfinance 代號（美股、韓股、匯率）。"""
    return not CODE_OK.match(code)


def sec_type_from_code(code):
    """只看代號的粗分類；正式分類以 companies.csv 為準。"""
    if code.startswith("00"):
        if code.endswith("L"):
            return "etf_leveraged"
        if code.endswith("R"):
            return "etf_inverse"
        if code.endswith("B"):
            return "etf_bond"
        return "etf"
    if re.fullmatch(r"\d{4}", code):
        return "stock"
    if re.fullmatch(r"\d{4}[A-Z]", code):
        return "preferred"
    return "other"


def _read_rows(p):
    import csv
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_rows(p, fields, rows):
    import csv
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k, "")) for k in fields})
    os.replace(tmp, p)


class DailyStore:
    """每日資料（有 date 欄）的存放：data/<name>.csv 放最近 recent_days 個交易日；
    完整歷史依月份存在 data/history/<name>/YYYY-MM.csv（舊月份不再變動，git 不重複存）。
    long_codes 的列（例如全市場合計 _MARKET）在近期檔保留 long_days 天。
    archive=False 時不分月，近期檔保留 keep_days（單一檔案模式）。"""

    def __init__(self, name, fields, key_cols, recent_days, keep_days, archive=True, long_codes=(), long_days=None):
        self.name, self.fields, self.key_cols = name, fields, key_cols
        self.recent_days, self.keep_days, self.archive = recent_days, keep_days, archive
        self.long_codes, self.long_days = set(long_codes), long_days or keep_days
        self.recent_path = path("data", f"{name}.csv")
        self.hist_dir = path("data", "history", name)
        self.rows = {}          # key -> row（近期檔 + 載入的月份檔）
        self.idx = {}           # (date, market) -> set(key)
        self.touched = set()    # 本次更新過的日期
        for r in _read_rows(self.recent_path):
            self._add(r)

    def _add(self, r, overwrite=True):
        k = self.key(r)
        if not overwrite and k in self.rows:
            return
        self.rows[k] = r
        self.idx.setdefault((r["date"], r.get("market")), set()).add(k)

    def key(self, r):
        return tuple(r[c] for c in self.key_cols)

    def month_file(self, ym):
        return os.path.join(self.hist_dir, f"{ym}.csv")

    def load_months(self, dates):
        """把涵蓋這些日期的月份檔載入（判斷是否已有資料用）。"""
        if not self.archive:
            return
        for ym in sorted({d[:7] for d in dates}):
            for r in _read_rows(self.month_file(ym)):
                self._add(r, overwrite=False)

    def has(self, date, market):
        return bool(self.idx.get((date, market)))

    def replace(self, date, market, new_rows):
        for k in self.idx.pop((date, market), set()):
            self.rows.pop(k, None)
        for r in new_rows:
            self._add(r)
        self.touched.add(date)

    def save(self):
        """寫近期檔；分月模式下另外寫本次更新過的月份檔，並刪除超過 keep_days 的舊月份檔。回傳近期檔日期數。"""
        all_dates = sorted({r["date"] for r in self.rows.values()})
        if self.archive:
            for ym in sorted({d[:7] for d in self.touched}):
                cur = {self.key(r): r for r in _read_rows(self.month_file(ym))}
                for k in [k for k, r in cur.items() if r["date"] in self.touched]:
                    del cur[k]
                for k, r in self.rows.items():
                    if r["date"][:7] == ym and r["date"] in self.touched:
                        cur[k] = r
                _write_rows(self.month_file(ym), self.fields, [cur[k] for k in sorted(cur)])
            hist_dates = set(all_dates)
            if os.path.isdir(self.hist_dir):
                for fn in os.listdir(self.hist_dir):
                    if fn.endswith(".csv"):
                        hist_dates |= {r["date"] for r in _read_rows(os.path.join(self.hist_dir, fn))}
            keep_from = sorted(hist_dates)[-self.keep_days:][0] if hist_dates else ""
            if os.path.isdir(self.hist_dir):
                for fn in os.listdir(self.hist_dir):
                    if fn.endswith(".csv") and fn[:7] < keep_from[:7]:
                        os.remove(os.path.join(self.hist_dir, fn))
            n_recent = self.recent_days
        else:
            n_recent = self.keep_days
        recent = set(all_dates[-n_recent:])
        long_keep = set(all_dates[-self.long_days:])
        out = [self.rows[k] for k in sorted(self.rows)
               if (self.rows[k]["date"] in long_keep if self.rows[k].get("code") in self.long_codes
                   else self.rows[k]["date"] in recent)]
        _write_rows(self.recent_path, self.fields, out)
        return len({r["date"] for r in out})


def write_json(p, obj, compact=False):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:   # 大檔：不縮排，減少約 4 成大小
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def read_json(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def log(*a):
    print(*a, file=sys.stderr, flush=True)
