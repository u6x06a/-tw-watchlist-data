"""全市場「日期 × 代號」矩陣，供選股（screen.py）、模擬帳戶（paper.py）、回測（backtest.py）共用同一套計算。

所有矩陣的列 = 台股交易日（由舊到新），欄 = 代號。某日沒有成交 = NaN。
價格為還原價（adjlib.load_adjusted）；指標以「截至該日」的資料計算，不偷看未來。
與 indicators.py 的差異：indicators.py 只算最新一天、以每檔自己的 K 棒序列計算；這裡以全市場交易日對齊，
停牌日為 NaN，均線等滾動指標在視窗內有 NaN 時為 NaN（只影響停牌或無成交的冷門股，流動性篩選會先排除）。
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adjlib import load_adjusted  # noqa: E402
from common import CODE_OK, load_config, path, read_json  # noqa: E402


def _read(name, **kw):
    p = path("data", f"{name}.csv")
    return pd.read_csv(p, dtype={"code": str, "date": str}, **kw) if os.path.exists(p) else None


def _history(name):
    """近期檔 + 分月歷史檔合併（同 key 以近期檔為準）。"""
    frames = []
    d = path("data", "history", name)
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".csv"):
                frames.append(pd.read_csv(os.path.join(d, fn), dtype={"code": str, "date": str}))
    r = _read(name)
    if r is not None:
        frames.append(r)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(["date", "market", "code"], keep="last")


class Panel:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        df = load_adjusted()
        raw = pd.read_csv(path("data", "prices.csv"), dtype={"code": str, "date": str},
                          usecols=["code", "date", "volume"]).rename(columns={"volume": "raw_volume"})
        df = df.merge(raw, on=["code", "date"], how="left")
        meta = read_json(path("data", "meta.json"), {}) or {}
        partial = meta.get("partial_bars") or {}
        if partial:
            bad = {(c, d) for c, ds in partial.items() for d in ds}
            df = df[~pd.Series(list(zip(df["code"], df["date"])), index=df.index).isin(bad)]
        tw = df[df["code"].map(lambda c: bool(CODE_OK.match(c)))]
        cnt = tw.groupby("date").size()
        # 交易日 = 全市場筆數達完整門檻的日子（只有關注名單的早期日子不算）
        full = cnt[cnt >= int(self.cfg["data"]["complete_min_rows"])].index
        tw = tw[tw["date"].isin(full)]
        self.dates = np.array(sorted(full))
        piv = lambda col: tw.pivot(index="date", columns="code", values=col).reindex(self.dates)  # noqa: E731
        self.O, self.H, self.L, self.C = piv("open"), piv("high"), piv("low"), piv("close")
        self.V, self.RV, self.RC = piv("volume"), piv("raw_volume"), piv("raw_close")
        self.UNC = piv("adj_uncertain").fillna(False).astype(bool)
        self.VFB = piv("vol_fallback").fillna(False).astype(bool)
        self.codes = list(self.C.columns)
        comp = _read("companies", keep_default_na=False)
        comp = comp.set_index("code") if comp is not None else pd.DataFrame()
        self.comp = comp
        self.sec_type = pd.Series({c: (comp.loc[c, "sec_type"] if c in comp.index else "") for c in self.codes})
        self.industry = pd.Series({c: (comp.loc[c, "industry"] if c in comp.index else "") for c in self.codes})
        self.full_delivery = pd.Series({c: (comp.loc[c, "full_delivery"] == "Y") if c in comp.index else False
                                        for c in self.codes})
        sh = pd.Series({c: pd.to_numeric(comp.loc[c, "shares_outstanding"], errors="coerce") if c in comp.index
                        else np.nan for c in self.codes})
        self.shares = sh
        # 籌碼（張）：外資 = 外資不含自營 + 外資自營
        inst = _history("institutional")
        if inst is not None:
            inst = inst[inst["code"] != "_MARKET"]
            inst = inst.assign(f=(inst["foreign_net"].astype(float) + inst["foreign_dealer_net"].fillna(0)) / 1000,
                               t=inst["trust_net"].astype(float) / 1000)
            self.FN = inst.pivot_table(index="date", columns="code", values="f", aggfunc="last").reindex(
                index=self.dates, columns=self.codes)
            self.TN = inst.pivot_table(index="date", columns="code", values="t", aggfunc="last").reindex(
                index=self.dates, columns=self.codes)
            self.inst_dates = set(inst["date"].unique())
        else:
            self.FN = self.TN = None
            self.inst_dates = set()
        mg = _history("margin")
        if mg is not None:
            m = mg[mg["code"] != "_MARKET"]
            self.MB = m.pivot_table(index="date", columns="code", values="margin_balance", aggfunc="last").reindex(
                index=self.dates, columns=self.codes)
            self.margin_dates = set(m["date"].unique())
        else:
            self.MB = None
            self.margin_dates = set()
        fh = _history("foreign_holding")
        self.FP = fh.pivot_table(index="date", columns="code", values="foreign_pct", aggfunc="last").reindex(
            index=self.dates, columns=self.codes) if fh is not None else None
        td = _read("tdcc")
        self.tdcc = td.pivot_table(index="date", columns="code", values="pct_1000plus", aggfunc="last") \
            if td is not None else None
        rev = pd.read_csv(path("data", "revenue.csv"), dtype={"code": str, "ym": str}) \
            if os.path.exists(path("data", "revenue.csv")) else None
        self.revenue = rev
        self.bench = self.cfg["market"]["bench"]
        self._cache = {}

    # ------------------------------------------------ 基本指標（整個矩陣）
    def _c(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def np(self, name):
        """矩陣的 numpy 版本（快取）。"""
        return self._c(("np", name), lambda: getattr(self, name).values)

    def np_of(self, key, df):
        return self._c(("np",) + tuple(key), lambda: df.values)

    def ma(self, n):
        return self._c(("ma", n), lambda: self.C.rolling(n).mean())

    def vma(self, n):
        return self._c(("vma", n), lambda: self.V.rolling(n).mean())

    def ret(self, n):
        return self._c(("ret", n), lambda: self.C / self.C.shift(n) - 1)

    def hh(self, n, col="H"):
        return self._c(("hh", n, col), lambda: getattr(self, col).rolling(n).max())

    def ll(self, n, col="L"):
        return self._c(("ll", n, col), lambda: getattr(self, col).rolling(n).min())

    def value20(self):
        return self._c("value20", lambda: (self.RC * self.RV).rolling(20).mean())

    def atr(self, n=14):
        """Wilder ATR（與 indicators.py 相同：第一個值 = 前 n 根 TR 平均，之後 (前值×(n−1) + TR) / n）。"""
        def f():
            pc = self.C.shift(1)
            tr = pd.concat([(self.H - self.L), (self.H - pc).abs(), (self.L - pc).abs()]).groupby(level=0).max()
            tr = tr.reindex(self.dates).where(pc.notna()).values   # 第一根沒有前收，不算 TR（與 indicators.py 相同）
            out = np.full(tr.shape, np.nan)
            cnt = np.zeros(tr.shape[1])
            acc = np.zeros(tr.shape[1])
            prev = np.full(tr.shape[1], np.nan)
            for i in range(tr.shape[0]):
                x = tr[i]
                ok = ~np.isnan(x)
                first = ok & (cnt < n)
                acc[first] += x[first]
                cnt[first] += 1
                init = first & (cnt == n)
                prev[init] = acc[init] / n
                upd = ok & ~first & ~np.isnan(prev)
                prev[upd] = (prev[upd] * (n - 1) + x[upd]) / n
                out[i] = np.where(cnt >= n, prev, np.nan)
            return pd.DataFrame(out, index=self.dates, columns=self.codes)
        return self._c(("atr", n), f)

    def bars(self):
        return self._c("bars", lambda: self.C.notna().cumsum())

    def rs_rank(self):
        """每日全市場 RS 百分位（0~100）：權重 × N 日報酬，以 config indicators.rs_rank.universe 的分布換算。"""
        def f():
            w = self.cfg["indicators"]["rs_rank"]["weights"]
            score = sum(float(wt) * self.ret(int(p)) for p, wt in w.items())
            univ = self.sec_type.isin(self.cfg["indicators"]["rs_rank"]["universe"]).values
            out = np.full(score.shape, np.nan)
            sv = score.values
            for i in range(sv.shape[0]):
                base = np.sort(sv[i, univ][~np.isnan(sv[i, univ])])
                if len(base):
                    ok = ~np.isnan(sv[i])
                    out[i, ok] = np.searchsorted(base, sv[i, ok], "right") / len(base) * 100
            return pd.DataFrame(out, index=self.dates, columns=self.codes)
        return self._c("rs", f)

    def industry_median(self, n):
        """每日各產業（普通股）n 日報酬中位數，展開成與代號同形狀的矩陣。"""
        def f():
            r = self.ret(n)
            stock = self.sec_type == "stock"
            out = pd.DataFrame(np.nan, index=self.dates, columns=self.codes)
            for ind, cols in self.industry[stock & (self.industry != "")].groupby(self.industry).groups.items():
                med = r[list(cols)].median(axis=1)
                members = self.industry[self.industry == ind].index
                out[members] = np.repeat(med.values[:, None], len(members), axis=1)
            return out
        return self._c(("indmed", n), f)

    def industry_rank(self):
        """每日產業排名（依 config indicators.industry.rank_by：rs_rank_median 或 ret20_median），1 = 最強。"""
        def f():
            ci = self.cfg["indicators"]["industry"]
            stock = (self.sec_type == "stock") & (self.industry != "")
            base = self.rs_rank() if ci["rank_by"] == "rs_rank_median" else self.ret(20)
            groups = self.industry[stock].groupby(self.industry[stock]).groups
            med = pd.DataFrame({ind: base[list(cols)].median(axis=1) for ind, cols in groups.items()
                                if len(cols) >= int(ci["min_members"])})
            rank = med.rank(axis=1, ascending=False, method="min")
            out = pd.DataFrame(np.nan, index=self.dates, columns=self.codes)
            for ind in rank.columns:
                members = self.industry[self.industry == ind].index
                out[members] = np.repeat(rank[ind].values[:, None], len(members), axis=1)
            return out
        return self._c("indrank", f)

    # ------------------------------------------------ 大盤（0050）逐日狀態
    def market_state(self):
        """每日：above_ma60、change_pct、panic（最近恐慌日高低點與是否跌破）、ladder level 與當日新觸發的階、狀態。"""
        def f():
            mc = self.cfg["market"]
            b = self.bench
            c, h, l = self.C[b].values, self.H[b].values, self.L[b].values
            ma60 = self.ma(int(mc["ma_long"]))[b].values
            chg = np.r_[np.nan, c[1:] / c[:-1] - 1] * 100
            rows = []
            hl = int(mc["high_lookback"])
            lad, pc = mc["ladder"], mc["panic"]
            for i in range(len(c)):
                if np.isnan(c[i]):
                    rows.append({})
                    continue
                lo_i = max(0, i - hl + 1)
                i_hi = lo_i + int(np.nanargmax(c[lo_i:i + 1]))
                steps = []
                for k in range(i_hi + 1, i + 1):
                    if chg[k] <= -float(lad["daily_drop_pct"]):
                        steps.append(("daily", k))
                for sp in lad["drawdown_steps_pct"]:
                    for k in range(i_hi + 1, i + 1):
                        if (c[k] / c[i_hi] - 1) * 100 <= -float(sp):
                            steps.append((f"dd{sp}", k))
                            break
                new = [s for s in steps if s[1] == i]
                panic = [k for k in range(max(1, i - int(pc["lookback_days"]) + 1), i + 1)
                         if chg[k] <= -float(pc["daily_drop_pct"])]
                below_panic = bool(panic and c[i] < l[panic[-1]])
                above = bool(c[i] > ma60[i]) if not np.isnan(ma60[i]) else None
                rows.append({"date": self.dates[i], "change_pct": chg[i], "above_ma60": above,
                             "ladder_level": len(steps), "ladder_new": [s[0] for s in new],
                             "panic_date": self.dates[panic[-1]] if panic else None, "below_panic_low": below_panic,
                             "filtered": (above is False) or below_panic, "high_date": self.dates[i_hi]})
            return rows
        return self._c("market", f)

    def idx(self, d):
        return int(np.searchsorted(self.dates, d))
