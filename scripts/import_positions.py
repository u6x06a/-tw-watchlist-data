#!/usr/bin/env python3
"""從「發財試算表」的交易明細頁，用先進先出（FIFO）算出目前還持有的每一批（lot），寫到本機 positions.local.json。

positions.local.json 是私人資料（持股、成本），只留在本機，已列入 .gitignore，不要 push 到公開 repo。
台股用成交單價；純英文字母的美股代號（VOO、QQQM、GOOG...）試算表只有台幣金額，改用「總收付金額-手續費」除以股數、
再除以成交日 USDTWD 收盤，換成美元進場價（currency=USD，含誤差，見 fx 欄位）。
備註含「拆」（例如 [1拆24]）的買進，成交價已是分割後基準，標 price_basis=split_adjusted。

用法：python scripts/import_positions.py "path/to/發財試算表.xlsx"
需要 openpyxl。
"""
import json
import os
import re
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "positions.local.json")
TZ_TW = timezone(timedelta(hours=8))


def norm(c):
    if c is None:
        return None
    if isinstance(c, float) and c == int(c):
        return str(int(c))
    return str(c).strip()


# 要略過的代號由命令列 --ignore A,B 指定（不寫在公開 repo）
IGNORE = set(sys.argv[sys.argv.index("--ignore") + 1].split(",")) if "--ignore" in sys.argv else set()
PRICES = os.path.join(ROOT, "data", "prices.csv")


def load_fx():
    """USDTWD=X 每日收盤，由舊到新的 [(date, close)]。"""
    import csv
    out = []
    if os.path.exists(PRICES):
        with open(PRICES, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["code"] == "USDTWD=X":
                    out.append((r["date"], float(r["close"])))
    return sorted(out)


def fx_asof(fx, date):
    best = None
    for d, c in fx:
        if d <= date:
            best = c
        else:
            break
    return best


def main(path):
    ws = openpyxl.load_workbook(path, data_only=True)["交易明細"]
    rows = []
    for i, r in enumerate(ws.iter_rows(min_row=2, max_col=10, values_only=True)):
        d, code, act, px, qty, fee, tax, amt, cls, note = r
        if d is None or act not in ("買入", "賣出") or code is None:
            continue
        rows.append((d.date().isoformat(), i, norm(code), act, px, qty, fee or 0, note or "", amt))
    rows.sort(key=lambda x: (x[0], x[1]))

    fx = load_fx()
    lots, excluded, problems = defaultdict(deque), set(), []
    for date, _, code, act, px, qty, fee, note, amt in rows:
        us = bool(re.fullmatch(r"[A-Za-z]+", code))
        if code in IGNORE:
            excluded.add(code)
            continue
        if qty is None or px is None:
            problems.append(f"{date} {code} {act} 缺股數或單價，略過")
            continue
        if act == "買入":
            if us:
                # 試算表的 D 欄有時是台幣單價、有時是總金額，不可靠；改用總收付金額 H 扣手續費，除以股數，
                # 再用成交日（或之前最近一天）的 USDTWD 收盤換成美元。券商實際匯率和 yfinance 收盤有差，約 ±1%。
                rate = fx_asof(fx, date)
                if rate is None or amt is None:
                    problems.append(f"{date} {code} 買入缺匯率或金額，略過")
                    continue
                twd_unit = (abs(amt) - fee) / qty
                lots[code].append({"date": date, "price": round(twd_unit / rate, 4), "shares": qty, "fee": fee,
                                   "currency": "USD", "twd_unit": round(twd_unit, 2), "fx": round(rate, 4), "price_basis": "raw"})
            else:
                lots[code].append({"date": date, "price": px, "shares": qty, "fee": fee,
                                   "price_basis": "split_adjusted" if "拆" in str(note) else "raw"})
        else:
            left = qty
            while left > 1e-9 and lots[code]:
                lot = lots[code][0]
                take = min(left, lot["shares"])
                lot["shares"] = round(lot["shares"] - take, 8)
                left = round(left - take, 8)
                if lot["shares"] <= 1e-9:
                    lots[code].popleft()
            if left > 1e-9:
                problems.append(f"{date} {code} 賣出 {qty} 超過持有，缺 {left}")
    entries = {c: list(q) for c, q in sorted(lots.items()) if q}
    doc = {"_readme": "私人資料，只留本機（已列入 .gitignore），不要 push。每批 = FIFO 剩餘的買進批次；price 是成交單價（未含手續費）。",
           "source": os.path.basename(path), "generated_at": datetime.now(TZ_TW).isoformat(timespec="seconds"),
           "method": "FIFO", "atr_mult": 2, "max_stop_pct": 10,
           "excluded_us": sorted(excluded), "problems": problems, "entries": entries}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"寫入 {OUT}：{len(entries)} 檔，{sum(len(v) for v in entries.values())} 批；排除美股 {sorted(excluded)}；問題 {len(problems)}",
          file=sys.stderr)
    for p in problems:
        print("  " + p, file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1])
