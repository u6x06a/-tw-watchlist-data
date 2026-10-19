"""由全市場日行情的「漲跌」欄反推交易所參考價，找出參考價 ≠ 前一日收盤的代號日（除權息、分割、減資、面額變更…）。

參考價 = 收盤 − 漲跌（證交所 MI_INDEX「漲跌(+/-)」+「漲跌價差」；櫃買 afterTrading/otc「漲跌」）。
  - 證交所除權息當天「漲跌(+/-)」顯示 X、漲跌價差為 0，無法反推 → 只記標記 X，參考價由除權息表（TWT49U）提供。
  - 櫃買除權息當天「漲跌」直接寫「除權息/除息/除權」→ 同上，參考價由 exDailyQ 提供。
結果存在 data/ref_events.csv（只存有事件的列，檔案很小），由 scripts/adjust.py 使用。
此模組被 fetch_prices.py 呼叫；任何錯誤都由呼叫端吞掉，不影響 prices.csv。
"""
import csv
import json
import os
import re
from bisect import bisect_left, bisect_right

HERE = os.path.dirname(os.path.abspath(__file__))
REF_EVENTS = os.path.join(os.path.dirname(HERE), "data", "ref_events.csv")
COVERAGE = os.path.join(os.path.dirname(HERE), "data", "ref_events_coverage.json")
FIELDS = ["code", "date", "market", "prev_date", "prev_close", "close", "change", "ref_price", "mark", "source"]


def parse_change(market, sign_cell, chg_cell):
    """回傳 (帶正負號的漲跌 或 None, 標記字串)。標記非空代表當天交易所標示除權息等，漲跌不可用來反推。"""
    if market == "TWSE":
        sign = re.sub(r"<[^>]+>", "", str(sign_cell or "")).strip()
        if sign == "X":
            return None, "X"
        try:
            v = float(str(chg_cell).replace(",", "").strip())
        except ValueError:
            return None, ""
        return (-v if sign == "-" else v), ""
    s = str(chg_cell or "").replace(",", "").strip()
    try:
        return float(s), ""
    except ValueError:
        # 「---」= 無成交/無漲跌；其他文字（除權息、除息、除權…）當作標記
        return None, ("" if s in ("", "-", "--", "---") else s)


def _load():
    out = {}
    if os.path.exists(REF_EVENTS):
        with open(REF_EVENTS, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["code"], r["date"])] = r
    return out


def update(merged, raw_rows, tol=0.005, source="bulk", complete_min_rows=1800):
    """merged: {(code, date): [o,h,l,c,v]}（已合併當次抓到的資料）；
    raw_rows: [{code,date,market,close,_chg,_mark}]（當次抓到、含漲跌欄的列）。
    只有在「前一筆資料日到當天之間沒有不完整的日子」時才比較參考價與前收；
    否則前一筆可能不是真正的前一個交易日（例如早期只有關注名單的日子），不比較。
    回傳這批列中記錄為事件的筆數。"""
    dates_by_code, n_by_date = {}, {}
    for (c, d) in merged:
        dates_by_code.setdefault(c, []).append(d)
        if re.match(r"^(\d{4}[A-Z]?|00\d{2,4}[A-Z]?)$", c):
            n_by_date[d] = n_by_date.get(d, 0) + 1
    for c in dates_by_code:
        dates_by_code[c].sort()
    all_dates = sorted(n_by_date)
    incomplete = [d for d in all_dates if n_by_date[d] < complete_min_rows]
    events = _load()
    touched = set()
    n = 0
    for r in raw_rows:
        code, date = r["code"], r["date"]
        touched.add((code, date))
        events.pop((code, date), None)  # 重抓時以新結果為準
        chg, mark = r.get("_chg"), r.get("_mark", "")
        ds = dates_by_code.get(code, [])
        i = bisect_left(ds, date)
        prev_date = ds[i - 1] if i > 0 else None
        prev_close = None
        if prev_date:
            j = bisect_right(incomplete, prev_date)   # 第一個晚於 prev_date 的不完整日
            if j < len(incomplete) and incomplete[j] < date:
                prev_date = None   # 中間有不完整的日子，無法確認前一個交易日
        if prev_date:
            try:
                prev_close = float(merged[(code, prev_date)][3])
            except (TypeError, ValueError):
                prev_close = None
        close = float(r["close"])
        ref = None if chg is None else round(close - chg, 4)
        if mark or (ref is not None and prev_close is not None and abs(ref - prev_close) > tol):
            events[(code, date)] = {
                "code": code, "date": date, "market": r.get("market", ""), "prev_date": prev_date or "",
                "prev_close": "" if prev_close is None else prev_close, "close": close,
                "change": "" if chg is None else chg, "ref_price": "" if ref is None else ref,
                "mark": mark, "source": source}
            n += 1
    tmp = REF_EVENTS + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for k in sorted(events):
            w.writerow(events[k])
    os.replace(tmp, REF_EVENTS)
    # 哪些「市場 × 日期」已經用漲跌欄檢查過（adjust.py 據此區分「沒有事件」與「沒檢查過」）
    cov = {}
    if os.path.exists(COVERAGE):
        with open(COVERAGE, encoding="utf-8") as f:
            cov = json.load(f)
    added = {}
    for r in raw_rows:
        added.setdefault(r.get("market", ""), set()).add(r["date"])
    for m, ds in added.items():
        cov[m] = sorted(set(cov.get(m, [])) | ds)
    with open(COVERAGE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cov, f, ensure_ascii=False)
    os.replace(COVERAGE + ".tmp", COVERAGE)
    return n
