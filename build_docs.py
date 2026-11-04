#!/usr/bin/env python3
import csv, json, os, sys, collections

CODES = ["00735", "2303", "3017", "3081", "3324", "3653", "6196", "NVDA"]
KEEP = 100

def main(csv_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    by = collections.defaultdict(list)
    for r in csv.DictReader(open(csv_path, encoding="utf-8")):
        by[r["code"]].append(r)
    for code in CODES:
        rs = sorted(by[code], key=lambda r: r["date"])
        if len(rs) < 62:
            print(code, "資料不足 62 筆，略過", file=sys.stderr)
            continue
        C = [float(r["close"]) for r in rs]
        V = [float(r["volume"]) / 1000 for r in rs]
        H = [float(r["high"]) for r in rs]
        L = [float(r["low"]) for r in rs]
        O = [float(r["open"]) for r in rs]
        n = len(C)
        def ma(k, end=n):
            return sum(C[end - k:end]) / k
        m = {k: ma(k) for k in (5, 10, 20, 60)}
        mp = {k: ma(k, n - 1) for k in (5, 10, 20, 60)}
        c, p = C[-1], C[-2]
        bias = {k: (c / m[k] - 1) * 100 for k in m}
        r5 = V[-1] / (sum(V[-6:-1]) / 5)
        r20 = V[-1] / (sum(V[-21:-1]) / 20)
        arr = "多頭排列" if m[5] > m[10] > m[20] > m[60] else ("空頭排列" if m[5] < m[10] < m[20] < m[60] else "均線糾結")
        tags = []
        if bias[20] >= 15:
            tags.append({"t": f"乖離過大 20日{bias[20]:+.1f}%", "k": "hot"})
        for k in (5, 10, 20, 60):
            if p < mp[k] and c >= m[k]:
                tags.append({"t": f"站上{k}日線", "k": "up"})
            if p >= mp[k] and c < m[k]:
                tags.append({"t": f"跌破{k}日線", "k": "down"})
        if c >= max(C[-20:]):
            tags.append({"t": "收盤創20日新高", "k": "up"})
        if bias[20] < 15:
            near = min((10, 20), key=lambda k: abs(bias[k]))
            if abs(bias[near]) <= 1.5:
                tags.append({"t": f"貼近{near}日線", "k": "flat"})
        if r5 >= 2:
            tags.append({"t": f"爆量 {r5:.1f}倍", "k": "hot"})
        elif r5 >= 1.5:
            tags.append({"t": f"放量 {r5:.1f}倍", "k": "hot"})
        elif r5 <= 0.6:
            tags.append({"t": f"量縮 {r5:.2f}倍", "k": "flat"})
        if not tags:
            tags.append({"t": "無特別訊號", "k": "flat"})
        doc = {
            "code": code, "date": rs[-1]["date"],
            "close": c, "prev": p, "chg": round(c - p, 2), "chgPct": round((c / p - 1) * 100, 2),
            "open": O[-1], "high": H[-1], "low": L[-1],
            "volLots": round(V[-1]), "volRatio5": round(r5, 2), "volRatio20": round(r20, 2),
            "ma": {"m5": round(m[5], 2), "m10": round(m[10], 2), "m20": round(m[20], 2), "m60": round(m[60], 2)},
            "bias": {"b5": round(bias[5], 2), "b10": round(bias[10], 2), "b20": round(bias[20], 2), "b60": round(bias[60], 2)},
            "arr": arr, "tags": tags,
            "hi20": max(H[-20:]), "lo20": min(L[-20:]), "hi60": max(H[-60:]), "low5": min(L[-5:]),
            "dates": [r["date"][5:] for r in rs[-KEEP:]],
            "closes": C[-KEEP:],
        }
        json.dump(doc, open(os.path.join(out_dir, f"calc_{code}.json"), "w", encoding="utf-8"), ensure_ascii=False)
        print(code, doc["date"], c, doc["chgPct"], arr, [t["t"] for t in tags])

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
