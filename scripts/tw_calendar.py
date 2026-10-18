"""台股交易日曆：exchange_calendars XTAI 與證交所「市場開休市日期」交叉核對。

證交所來源（已實測）：https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=YYYY0101&response=json
  data: [日期, 名稱, 說明]。名稱含「交易日」且不含「無交易」者（如「國曆新年開始交易日」「農曆春節前最後交易日」）
  是有交易的提示日；其餘列（國定假日、「市場無交易，僅辦理結算交割作業」）為休市。
快取：data/holidays_twse.json，超過 config calendar.holiday_cache_days 天重抓；抓不到就用快取。
颱風等臨時休市不在表內，由資料是否出現判斷（scripts/market_status.py）。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import get, log, now_tw, path, read_json, write_json  # noqa: E402

CACHE = path("data", "holidays_twse.json")


def _is_closed(name):
    return not ("交易日" in name and "無交易" not in name)


def load_twse_holidays(years, cache_days=7, sleep=3.0, session=None):
    """回傳 ({'YYYY-MM-DD': 名稱} 休市日, 狀態字串)。"""
    import requests
    cache = read_json(CACHE, {}) or {}
    status = []
    s = session or requests.Session()
    for y in sorted(set(years)):
        ent = cache.get(str(y))
        fresh = ent and (datetime.now().timestamp() - ent.get("fetched_ts", 0)) < cache_days * 86400
        if fresh:
            status.append(f"{y}:cache")
            continue
        try:
            j = get(s, "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule",
                    {"date": f"{y}0101", "response": "json"}, sleep=sleep).json()
            rows = j.get("data") or []
            if not rows or not str(rows[0][0]).startswith(str(y)):
                raise ValueError(f"年份不符或沒有資料：{j.get('title')}")
            cache[str(y)] = {"fetched_ts": datetime.now().timestamp(),
                             "fetched_at": now_tw().isoformat(timespec="seconds"), "title": j.get("title"),
                             "closed": {r[0]: r[1] for r in rows if _is_closed(r[1])},
                             "trading_notes": {r[0]: r[1] for r in rows if not _is_closed(r[1])}}
            status.append(f"{y}:fetched")
        except Exception as e:  # noqa: BLE001
            status.append(f"{y}:fetch_failed({type(e).__name__})" + (",cache" if ent else ",no_cache"))
    write_json(CACHE, cache)
    closed = {}
    for y in years:
        closed.update((cache.get(str(y)) or {}).get("closed", {}))
    return closed, ";".join(status)


def xtai():
    try:
        import exchange_calendars as xcals
        return xcals.get_calendar("XTAI")
    except Exception as e:  # noqa: BLE001
        log(f"XTAI 日曆不可用：{e}")
        return None


def sessions(start, end, cache_days=7, sleep=3.0, extra_closed=()):
    """start..end（date）之間的台股交易日（由舊到新），以及兩個日曆不一致的日子。
    判斷：平日、證交所休市表沒有列為休市、不在 extra_closed（config calendar.tw_extra_closed，
    交易所資料確認沒有交易的臨時休市日），且 XTAI 認為是交易日（XTAI 不可用或超出範圍時只看前三項）。"""
    extra = {str(x) for x in (extra_closed or [])}
    import pandas as pd
    closed, st = load_twse_holidays(range(start.year, end.year + 1), cache_days, sleep)
    cal = xtai()
    out, conflicts = [], []
    d = start
    while d <= end:
        if d.weekday() < 5:
            iso = d.isoformat()
            sched_open = iso not in closed
            twse_open = sched_open and iso not in extra
            x = None
            if cal is not None:
                try:
                    x = bool(cal.is_session(pd.Timestamp(iso)))
                except Exception:  # noqa: BLE001
                    x = None
            if x is not None and x != sched_open:
                conflicts.append({"date": iso, "xtai": x, "twse_schedule": sched_open, "twse_name": closed.get(iso)})
            if twse_open and x is not False:
                out.append(d)
        d += timedelta(days=1)
    return out, conflicts, st


def previous_sessions(n, today=None, cache_days=7, sleep=3.0, extra_closed=()):
    """今天（含）往回的最近 n 個交易日。"""
    today = today or now_tw().date()
    span = int(n * 1.6) + 30
    ss, conflicts, st = sessions(today - timedelta(days=span), today, cache_days, sleep, extra_closed)
    return ss[-n:], conflicts, st


if __name__ == "__main__":
    ss, cf, st = previous_sessions(10)
    print([d.isoformat() for d in ss], cf, st)
