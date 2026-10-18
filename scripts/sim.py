"""規則模擬引擎（階段 4、5 共用）：完全照規則買賣，不做主觀判斷。

時序（避免偷看未來）：第 t 日收盤 → 用 rules.evaluate(P, t) 產生訊號、檢查出場條件 → 掛單 → 第 t+1 日開盤價成交。
價格用還原價（含息），所以帳上報酬已包含配息；實際成交價會不同，數字僅反映規則本身。
帳戶分兩個：個股帳戶（R1、L1、L2、C1）與 ETF 帳戶（E1），各自計算總資產、部位上限與現金下限。

規則細節（config paper 區塊）與解讀：
  部位大小：股數 = 帳戶總資產 × risk_per_trade_pct ÷ (成交價 − 停損價)，取整數（允許零股）；金額低於 min_order_value 不買；
           再受單一個股 ≤ max_position_pct、單一產業 ≤ max_industry_pct、現金 ≥ min_cash_pct 限制，超過就減少股數或不買。
  R1 停損：以實際進場價（隔日開盤成交價）計算 max(進場 − 2×ATR, 進場 × 0.9)，每股風險 R = 進場 − 停損。
  R1 出場：收盤 < 停損 → 隔日全出；當日 market_wide_selloff → 先出一半，隔日收盤仍 < 停損再出剩下。
           收盤 ≥ 進場 + 1.5R → 賣 1/3（以原始股數計）；≥ +3R → 再賣 1/3；兩次都賣過後，剩餘部位收盤 < MA10 出場。
           時間停損：持有滿 time_stop_days 個交易日時，期間最高價未超過進場訊號 K 棒高點、且報酬在 ±3% 內 → 賣一半（一次）。
           該股出現 limit_up_then_heavy_black 旗標 → 賣一半（一次）。
  L1（預設 needs_news_check 不執行，可在 config 打開比較）：失效價（下跌前 60 日最低）或平均成本 −15% → 全出、停止加碼；
           分批：第一批止穩時；第二批 收盤 ≤ 第一批 − 1×ATR 或收盤由下往上站回 MA5；第三批 收盤站回 MA20；最多 3 批，
           總股數上限 = 依部位大小規則以失效價計算的股數。收盤達恐慌前高點（下跌起點的 20 日高點）賣 1/3，剩餘改用 R1 出場規則。
  E1：每觸發一階，投入 ETF 帳戶總資產 step_budget_pct%，依 ratio 分配 0050 / 00685L；不設停損。
      0050 收盤回到本輪高點（階梯歸零）時，若 00685L 市值占比 > max_lev_ratio，超出部分換回 0050。
"""
import numpy as np

from rules import evaluate


class Pos:
    def __init__(self, code, rule, industry, sec_type):
        self.code, self.rule, self.industry, self.sec_type = code, rule, industry, sec_type
        self.shares = 0
        self.cost = 0.0            # 總成本（含手續費）
        self.entry_i = None
        self.entry_price = None    # 第一筆成交價
        self.stop = None
        self.risk = None           # 每股初始風險（成交價 − 停損）
        self.orig_shares = 0
        self.signal_high = None
        self.flags = set()         # 已執行過的一次性動作
        self.batches = 0
        self.max_shares = None
        self.take_level = None     # L1：賣 1/3 的價位（恐慌前高點）
        self.mode = rule           # L1 賣 1/3 後改用 R1 出場
        self.pending_half_check = False
        self.realized = 0.0        # 已實現損益（含成本）
        self.trade_id = None
        self.chips_confirm = None   # 進場訊號當時的籌碼標記（回測分組統計用）
        self.chips_warning = None

    @property
    def avg(self):
        return self.cost / self.shares if self.shares else 0.0


class Account:
    def __init__(self, name, cash, cfg):
        self.name, self.cash, self.cfg = name, float(cash), cfg
        self.pos = {}
        self.nav = []

    def equity(self, prices):
        return self.cash + sum(p.shares * prices.get(c, p.avg) for c, p in self.pos.items())


class Sim:
    def __init__(self, P, cfg, opts=None, start_i=None, end_i=None):
        self.P, self.cfg = P, cfg
        self.opts = opts or {}
        pc = cfg["paper"]
        self.pc = pc
        cap = float(pc["initial_capital"])
        self.stock = Account("stock", cap * (1 - float(pc["etf_budget_ratio"])), cfg)
        self.etf = Account("etf", cap * float(pc["etf_budget_ratio"]), cfg)
        self.trades = []           # 每一筆成交
        self.closed = []           # 已結束的部位（統計用）
        self.recorded = []         # 有訊號但未執行
        self.orders = []           # 待隔日開盤成交：(account, code, side, shares or None, reason, meta)
        self.start_i = start_i if start_i is not None else int(np.searchsorted(P.dates, pc["start_date"]))
        self.end_i = end_i if end_i is not None else len(P.dates) - 1
        self.bench_shares = None
        self.bench_nav = []
        self.next_tid = 1
        self.signals_by_day = {}

    # ---------------------------------------------------- 成本
    def fee(self, value):
        c = self.pc["costs"]
        return max(value * float(c["fee_rate"]) * float(c["fee_discount"]), float(c["min_fee"])) if value > 0 else 0.0

    def tax(self, value, sec_type):
        c = self.pc["costs"]
        return value * float(c["tax_etf"] if str(sec_type).startswith("etf") else c["tax_stock"])

    # ---------------------------------------------------- 價格
    def px(self, mat, i, code):
        v = mat[code].iat[i] if code in mat.columns else np.nan
        return None if np.isnan(v) else float(v)

    def prices(self, i):
        out = {}
        for acc in (self.stock, self.etf):
            for c in acc.pos:
                p = self.px(self.P.C, i, c)
                if p is None:   # 停牌：沿用最近收盤
                    k = i
                    while p is None and k > 0:
                        k -= 1
                        p = self.px(self.P.C, k, c)
                out[c] = p or acc.pos[c].avg
        return out

    # ---------------------------------------------------- 成交
    def buy(self, acc, i, code, shares, rule, reason, meta):
        P = self.P
        price = self.px(P.O, i, code)
        if price is None or shares <= 0:
            return False
        value = shares * price
        f = self.fee(value)
        if value + f > acc.cash:
            shares = int((acc.cash - self.fee(acc.cash)) // price)
            if shares <= 0:
                return False
            value, f = shares * price, self.fee(shares * price)
        p = acc.pos.get(code)
        if p is None:
            p = Pos(code, rule, P.industry.get(code) or "", P.sec_type.get(code) or "")
            p.entry_i, p.entry_price = i, price
            p.trade_id = self.next_tid
            self.next_tid += 1
            acc.pos[code] = p
        p.shares += shares
        p.cost += value + f
        acc.cash -= value + f
        p.batches += 1
        for k, v in (meta or {}).items():
            if v is not None and getattr(p, k, None) is None:
                setattr(p, k, v)
        if p.stop is not None and p.risk is None:
            p.risk = price - p.stop
        p.orig_shares = max(p.orig_shares, p.shares)
        self.trades.append({"date": P.dates[i], "account": acc.name, "code": code, "side": "buy", "shares": int(shares),
                            "price": round(price, 4), "fee": round(f, 0), "tax": 0, "rule": rule, "reason": reason,
                            "trade_id": p.trade_id})
        return True

    def sell(self, acc, i, code, shares, reason):
        P = self.P
        p = acc.pos.get(code)
        price = self.px(P.O, i, code)
        if p is None or price is None:
            return False
        shares = min(int(shares), p.shares)
        if shares <= 0:
            return False
        value = shares * price
        f, tx = self.fee(value), self.tax(value, p.sec_type)
        cost_part = p.avg * shares
        p.realized += value - f - tx - cost_part
        p.cost -= cost_part
        p.shares -= shares
        acc.cash += value - f - tx
        self.trades.append({"date": P.dates[i], "account": acc.name, "code": code, "side": "sell", "shares": shares,
                            "price": round(price, 4), "fee": round(f, 0), "tax": round(tx, 0), "rule": p.rule,
                            "reason": reason, "trade_id": p.trade_id})
        if p.shares == 0:
            risk_total = (p.risk or 0) * p.orig_shares
            self.closed.append({"trade_id": p.trade_id, "code": code, "rule": p.rule, "entry_date": P.dates[p.entry_i],
                                "exit_date": P.dates[i], "hold_days": i - p.entry_i, "pnl": round(p.realized, 0),
                                "r_multiple": round(p.realized / risk_total, 3) if risk_total > 0 else None,
                                "exit_reason": reason, "chips_confirm": p.chips_confirm, "chips_warning": p.chips_warning})
            del acc.pos[code]
        return True

    # ---------------------------------------------------- 部位大小
    def size(self, acc, i, code, entry, stop, eq, prices, industry):
        sz = self.pc["sizing"]
        if entry is None or stop is None or entry <= stop:
            return 0
        sh = int(eq * float(sz["risk_per_trade_pct"]) / 100 / (entry - stop))
        cap_val = eq * float(sz["max_position_pct"]) / 100
        held = acc.pos.get(code)
        held_val = held.shares * prices.get(code, entry) if held else 0
        sh = min(sh, int(max(0, cap_val - held_val) // entry))
        ind_val = sum(p.shares * prices.get(c, p.avg) for c, p in acc.pos.items() if p.industry and p.industry == industry)
        if industry:
            sh = min(sh, int(max(0, eq * float(sz["max_industry_pct"]) / 100 - ind_val) // entry))
        free = acc.cash - eq * float(sz["min_cash_pct"]) / 100
        sh = min(sh, int(max(0, free) // (entry * (1 + float(self.pc["costs"]["fee_rate"])))))
        return max(sh, 0)

    # ---------------------------------------------------- 主迴圈
    def run(self, progress=None):
        P = self.P
        b = P.bench
        for i in range(self.start_i, self.end_i + 1):
            # 1) 開盤：執行昨日收盤的掛單（先賣後買）
            orders, self.orders = self.orders, []
            prices_prev = self.prices(i - 1) if i > 0 else {}
            for o in [o for o in orders if o["side"] == "sell"]:
                if not self.sell(o["acc"], i, o["code"], o["shares"], o["reason"]):
                    if o["acc"].pos.get(o["code"]):
                        self.orders.append(o)   # 停牌等無法成交 → 下一日再試
            for o in [o for o in orders if o["side"] == "buy"]:
                acc = o["acc"]
                eq = acc.equity(prices_prev)
                entry = self.px(P.O, i, o["code"])
                if entry is None:
                    continue
                if o.get("budget") is not None:          # E1：以金額下單
                    sh = int(min(o["budget"], acc.cash) // (entry * (1 + float(self.pc["costs"]["fee_rate"]))))
                else:
                    if o.get("atr") and o["rule"] == "R1":
                        # 停損以「實際進場價」（隔日開盤成交價）計算：max(進場 − k×ATR, 進場 × (1 − 上限%))
                        r1 = self.cfg["screen"]["r1"]
                        o["stop"] = max(entry - float(r1["stop_atr_mult"]) * o["atr"],
                                        entry * (1 - float(r1["stop_max_pct"]) / 100))
                        o["meta"]["stop"] = o["stop"]
                    if o["stop"] is not None and entry <= o["stop"]:
                        self.recorded.append({**o["rec"], "not_executed": "開盤價已低於停損"})
                        continue
                    sh = self.size(acc, i, o["code"], entry, o["stop"], eq, prices_prev, P.industry.get(o["code"]))
                    if o.get("max_total") is not None:
                        held = acc.pos[o["code"]].shares if o["code"] in acc.pos else 0
                        sh = min(sh, max(0, o["max_total"] - held))
                if sh <= 0:
                    self.recorded.append({**o["rec"], "not_executed": "部位上限 / 現金不足"})
                    continue
                if sh * entry < float(self.pc["sizing"]["min_order_value"]):
                    self.recorded.append({**o["rec"], "not_executed": "金額低於 min_order_value"})
                    continue
                self.buy(acc, i, o["code"], sh, o["rule"], o["reason"], o.get("meta"))
            if self.bench_shares is None:
                bp = self.px(P.O, i, b)
                if bp:
                    self.bench_shares = float(self.pc["initial_capital"]) / bp
            # 2) 收盤：訊號、出場檢查
            r = evaluate(P, i, self.opts.get("rules"))
            self.signals_by_day[P.dates[i]] = {s["rule"]: 0 for s in r["signals"]}
            flagged = {f["code"]: set(f["flags"]) for f in r["flags"]}
            sell_off = r["market_wide_selloff"]
            prices = self.prices(i)
            self._exits(i, prices, flagged, sell_off)
            self._entries(i, r, prices)
            self._e1(i, r, prices)
            for acc in (self.stock, self.etf):
                acc.nav.append(acc.equity(prices))
            cb = self.px(P.C, i, b)
            self.bench_nav.append(self.bench_shares * cb if self.bench_shares and cb else None)
            if progress and (i - self.start_i) % 20 == 0:
                progress(i)
        return self

    def _order_sell(self, acc, code, shares, reason):
        if any(o["side"] == "sell" and o["code"] == code and o["acc"] is acc for o in self.orders):
            return
        self.orders.append({"acc": acc, "side": "sell", "code": code, "shares": shares, "reason": reason})

    def _exits(self, i, prices, flagged, sell_off):
        P, rc, lc = self.P, self.pc["r1_exit"], self.pc["l1_exit"]
        for code, p in list(self.stock.pos.items()):
            c = self.px(P.C, i, code)
            if c is None:
                continue
            held = i - p.entry_i
            if p.mode == "L1":
                if (p.stop is not None and c < p.stop) or c < p.avg * (1 - float(lc["cost_stop_pct"]) / 100):
                    self._order_sell(self.stock, code, p.shares, "L1 失效價或平均成本 −15%")
                    continue
                if p.take_level is not None and c >= p.take_level:
                    self._order_sell(self.stock, code, max(1, p.shares // 3), "L1 反彈至恐慌前高點，賣 1/3，剩餘改右側規則")
                    p.mode = "R1"
                    p.risk = p.risk or (p.avg - (p.stop or 0))
                    continue
                atr = P.atr()[code].iat[i]
                ma5, ma5p, ma20 = P.ma(5)[code].iat[i], P.ma(5)[code].iat[i - 1], P.ma(20)[code].iat[i]
                cprev = self.px(P.C, i - 1, code)
                if p.batches < int(lc["max_batches"]) and p.shares < (p.max_shares or 0):
                    add = None
                    if p.batches == 1 and (c <= p.entry_price - atr or (cprev is not None and cprev <= ma5p and c > ma5)):
                        add = "L1 第二批"
                    elif p.batches == 2 and c > ma20:
                        add = "L1 第三批（站回 MA20）"
                    if add:
                        self.orders.append({"acc": self.stock, "side": "buy", "code": code, "stop": p.stop, "rule": "L1",
                                            "reason": add, "max_total": p.max_shares,
                                            "rec": {"date": P.dates[i], "code": code, "rule": "L1"}})
                continue
            # R1 出場規則
            if p.pending_half_check:
                p.pending_half_check = False
                if c < p.stop:
                    self._order_sell(self.stock, code, p.shares, "集體下跌日後隔日收盤仍低於停損，出剩下")
                    continue
            if c < p.stop:
                if sell_off and "selloff_half" not in p.flags:
                    p.flags.add("selloff_half")
                    p.pending_half_check = True
                    self._order_sell(self.stock, code, max(1, p.shares // 2), "跌破停損但為集體下跌日，先出一半")
                else:
                    self._order_sell(self.stock, code, p.shares, "收盤跌破停損")
                continue
            R = p.risk or 0
            if R > 0 and "p1" not in p.flags and c >= p.entry_price + float(rc["partial_1_r"]) * R:
                p.flags.add("p1")
                self._order_sell(self.stock, code, max(1, p.orig_shares // 3), f"+{rc['partial_1_r']}R 賣 1/3")
                continue
            if R > 0 and "p1" in p.flags and "p2" not in p.flags and c >= p.entry_price + float(rc["partial_2_r"]) * R:
                p.flags.add("p2")
                self._order_sell(self.stock, code, max(1, p.orig_shares // 3), f"+{rc['partial_2_r']}R 再賣 1/3")
                continue
            if "p2" in p.flags and c < P.ma(int(rc["trail_ma"]))[code].iat[i]:
                self._order_sell(self.stock, code, p.shares, f"收盤跌破 MA{rc['trail_ma']}")
                continue
            if held == int(rc["time_stop_days"]) and "time" not in p.flags:
                p.flags.add("time")
                hi = P.H[code].iloc[p.entry_i: i + 1].max()
                if (p.signal_high is None or hi <= p.signal_high) and abs(c / p.entry_price - 1) * 100 <= float(rc["time_stop_band_pct"]):
                    self._order_sell(self.stock, code, max(1, p.shares // 2), "時間停損：5 日未創新高且報酬在 ±3% 內，賣一半")
                    continue
            if "limit_up_then_heavy_black" in flagged.get(code, set()) and "lu_black" not in p.flags:
                p.flags.add("lu_black")
                self._order_sell(self.stock, code, max(1, p.shares // 2), "漲停隔天爆量收黑，賣一半")

    def _entries(self, i, r, prices):
        P, ex = self.P, self.pc["execute"]
        for s in r["signals"]:
            rule = s["rule"]
            if rule == "E1":
                continue
            rec = {"date": s["data_date"], "code": s["code"], "rule": rule}
            why = None
            if not ex.get(rule):
                why = f"config paper.execute.{rule}=false"
            elif s.get("needs_news_check") and not ex.get("needs_news_check"):
                why = "needs_news_check（預設不執行）"
            elif s.get("market_filtered") and ex.get("skip_market_filtered"):
                why = "大盤過濾中"
            elif rule == "L1" and s.get("status") != "可分批第一批":
                why = "L1 跌深觀察（未止穩）"
            elif s["code"] in self.stock.pos or any(o["code"] == s["code"] and o["side"] == "buy" for o in self.orders):
                why = "已持有或已掛單"
            elif s.get("stop") is None:
                why = "沒有停損價"
            if why:
                self.recorded.append({**rec, "not_executed": why})
                continue
            meta = {"stop": s["stop"], "signal_high": float(P.H[s["code"]].iat[i]),
                    "chips_confirm": s.get("chips_confirm"), "chips_warning": s.get("chips_warning")}
            if rule == "L1":
                eq = self.stock.equity(prices)
                e = s["entry_ref"]
                total = int(eq * float(self.pc["sizing"]["risk_per_trade_pct"]) / 100 / (e - s["stop"])) if e > s["stop"] else 0
                meta.update({"max_shares": total, "take_level": s["key"]["high_20d"]})
                self.orders.append({"acc": self.stock, "side": "buy", "code": s["code"], "stop": s["stop"], "rule": rule,
                                    "reason": "L1 第一批（止穩）", "meta": meta, "max_total": max(1, total // 3), "rec": rec})
            else:
                self.orders.append({"acc": self.stock, "side": "buy", "code": s["code"], "stop": s["stop"], "rule": rule,
                                    "reason": f"{rule} 訊號", "meta": meta, "rec": rec,
                                    "atr": (s.get("key") or {}).get("atr14")})

    def _e1(self, i, r, prices):
        P, e1 = self.P, self.pc["e1"]
        if not self.pc["execute"].get("E1"):
            return
        sigs = [s for s in r["signals"] if s["rule"] == "E1"]
        acc = self.etf
        eq = acc.equity(prices)
        if sigs:
            ratio = self.cfg["screen"]["e1"]["ratio"]
            tot = sum(float(v) for v in ratio.values())
            budget = eq * float(e1["step_budget_pct"]) / 100
            for s in sigs:
                w = float(ratio.get(s["code"], 0)) / tot
                self.orders.append({"acc": acc, "side": "buy", "code": s["code"], "stop": None, "rule": "E1",
                                    "reason": f"543 階梯第 {s['key']['ladder_level']} 階 {s['key']['new_steps']}",
                                    "budget": budget * w, "rec": {"date": s["data_date"], "code": s["code"], "rule": "E1"}})
        mk = r["market"]
        if mk.get("ladder_level") == 0 and "00685L" in acc.pos:
            lev_val = acc.pos["00685L"].shares * prices.get("00685L", 0)
            if eq > 0 and lev_val / eq > float(e1["max_lev_ratio"]):
                excess = lev_val - eq * float(e1["max_lev_ratio"])
                sh = int(excess // max(prices.get("00685L", 1), 1e-9))
                if sh > 0:
                    self._order_sell(acc, "00685L", sh, "0050 回到前高，00685L 比例超過上限，超出部分換回 0050")
                    self.orders.append({"acc": acc, "side": "buy", "code": "0050", "stop": None, "rule": "E1",
                                        "reason": "換回 0050", "budget": excess * 0.99,
                                        "rec": {"date": P.dates[i], "code": "0050", "rule": "E1"}})

    # ---------------------------------------------------- 結果
    def report(self, min_n=20):
        P = self.P
        dates = list(P.dates[self.start_i: self.end_i + 1])
        tot = [a + b for a, b in zip(self.stock.nav, self.etf.nav)]
        cap = float(self.pc["initial_capital"])

        def mdd(x):
            x = np.array([v for v in x if v is not None], float)
            if not len(x):
                return None
            peak = np.maximum.accumulate(x)
            return round(float(((x / peak) - 1).min() * 100), 2)
        stats = {}
        for rule in sorted({c["rule"] for c in self.closed}):
            cs = [c for c in self.closed if c["rule"] == rule]
            rs = [c["r_multiple"] for c in cs if c["r_multiple"] is not None]
            stats[rule] = {"trades": len(cs), "win_rate_pct": round(sum(c["pnl"] > 0 for c in cs) / len(cs) * 100, 1),
                           "avg_r": round(float(np.mean(rs)), 3) if rs else None,
                           "avg_hold_days": round(float(np.mean([c["hold_days"] for c in cs])), 1),
                           "max_loss": min(c["pnl"] for c in cs), "total_pnl": round(sum(c["pnl"] for c in cs), 0),
                           "sample_note": "樣本不足，僅供參考" if len(cs) < min_n else ""}
        prices = self.prices(self.end_i)
        positions = []
        for acc in (self.stock, self.etf):
            for c, p in acc.pos.items():
                positions.append({"account": acc.name, "code": c, "rule": p.rule, "shares": p.shares,
                                  "avg_cost": round(p.avg, 4), "stop": round(p.stop, 4) if p.stop else None,
                                  "entry_date": P.dates[p.entry_i], "close": round(prices.get(c, 0), 4),
                                  "unrealized": round(p.shares * prices.get(c, 0) - p.cost, 0), "batches": p.batches})
        bn = [v for v in self.bench_nav if v]
        return {
            "start_date": dates[0] if dates else None, "end_date": dates[-1] if dates else None,
            "initial_capital": cap,
            "total_return_pct": round((tot[-1] / cap - 1) * 100, 2) if tot else None,
            "bench_return_pct": round((bn[-1] / cap - 1) * 100, 2) if bn else None,
            "max_drawdown_pct": mdd(tot), "bench_max_drawdown_pct": mdd(self.bench_nav),
            "accounts": {a.name: {"equity": round(a.nav[-1], 0) if a.nav else None, "cash": round(a.cash, 0),
                                  "max_drawdown_pct": mdd(a.nav)} for a in (self.stock, self.etf)},
            "stats_by_rule": stats, "positions": positions,
            "nav": [{"date": d, "stock": round(s, 0), "etf": round(e, 0), "total": round(s + e, 0),
                     "bench_0050": round(b, 0) if b else None}
                    for d, s, e, b in zip(dates, self.stock.nav, self.etf.nav, self.bench_nav)],
            "trades": self.trades, "closed_trades": self.closed,
            "not_executed_counts": _count(self.recorded), "pending_orders": len(self.orders)}


def _count(rec):
    out = {}
    for r in rec:
        k = f"{r['rule']}：{r['not_executed']}"
        out[k] = out.get(k, 0) + 1
    return out
