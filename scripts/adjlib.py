"""即時計算還原價：data/prices.csv（原始）+ data/adj_factors.csv（還原係數）。全市場還原價不存檔。

用法（給指標、選股、回測）：
    from adjlib import load_adjusted
    df = load_adjusted()            # 全部代號；或 load_adjusted(codes={"0050", "2330"})
欄位：code,date,open,high,low,close,volume（台股為還原值，float）、raw_close（原始收盤）、
      adj_uncertain（True = 這一根的漲跌受「還原基準有疑義」的事件影響，例如現金增資除權；
                     config adjust.skip_adj_uncertain 為 true 時，選股與回測應略過這些 K 棒的訊號）、
      vol_fallback（True = 該列成交量來自逐檔備援來源，口徑與全市場不同，量比等計算應排除或標記）
config adjust.bad_rows 列出的 K 棒直接剔除。GLOBAL（美股/韓股/匯率）不還原。
"""
import csv
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE_OK, load_config, path  # noqa: E402

PRICES = path("data", "prices.csv")
FACTORS = path("data", "adj_factors.csv")
FALLBACK = path("data", "price_fallback_rows.csv")


def apply_factors(df, events):
    """df 須依 code、date 排序。events：可迭代的 dict（code,date,factor,volume_factor）。
    回傳 (價格係數, 成交量係數) 兩個 numpy 陣列：事件日以前的列乘上該事件係數（累乘）。"""
    pf, vf = np.ones(len(df)), np.ones(len(df))
    groups = df.groupby("code", sort=False).indices
    dates_all = df["date"].values
    for ev in events:
        idx = groups.get(ev["code"])
        if idx is None:
            continue
        k = np.searchsorted(dates_all[idx], ev["date"])
        pf[idx[:k]] *= float(ev["factor"])
        vf[idx[:k]] *= float(ev.get("volume_factor") or 1.0)
    return pf, vf


def load_adjusted(codes=None):
    cfg = load_config()["adjust"]
    df = pd.read_csv(PRICES, dtype={"code": str, "date": str})
    if codes is not None:
        df = df[df["code"].isin(set(codes))]
    bad = {(b["code"], b["date"]) for b in (cfg.get("bad_rows") or [])}
    if bad:
        df = df[~pd.Series(list(zip(df["code"], df["date"])), index=df.index).isin(bad)]
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    fac = pd.read_csv(FACTORS, dtype={"code": str, "date": str}) if os.path.exists(FACTORS) else pd.DataFrame()
    events = fac.to_dict("records") if len(fac) else []
    is_tw = df["code"].map(lambda c: bool(CODE_OK.match(c))).values
    pf, vf = apply_factors(df, events)
    pf[~is_tw], vf[~is_tw] = 1.0, 1.0
    df["raw_close"] = df["close"]
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col] * pf).round(4)
    df["volume"] = (df["volume"] * vf).round(0)
    # adj_uncertain：疑義事件的事件日（或之後第一根）
    df["adj_uncertain"] = False
    if len(fac) and "adj_uncertain" in fac.columns:
        unc = fac[fac["adj_uncertain"].astype(str) == "Y"]
        groups = df.groupby("code", sort=False).indices
        for ev in unc.itertuples(index=False):
            idx = groups.get(ev.code)
            if idx is None:
                continue
            k = np.searchsorted(df["date"].values[idx], ev.date)
            if k < len(idx):
                df.loc[idx[k], "adj_uncertain"] = True
    df["vol_fallback"] = False
    if os.path.exists(FALLBACK):
        with open(FALLBACK, newline="", encoding="utf-8") as f:
            fb = {(r["code"], r["date"]) for r in csv.DictReader(f)}
        if fb:
            df["vol_fallback"] = pd.Series(list(zip(df["code"], df["date"]))).isin(fb).values
    return df
