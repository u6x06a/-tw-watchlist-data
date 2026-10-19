#!/usr/bin/env python3
"""階段 6：執行一個流程步驟並記錄到 data/meta.json 的 pipeline 區塊（狀態、開始/結束時間、耗時、輸出檔的 data_date）。

用法：python scripts/run_step.py <步驟名稱> <指令...>
  例：python scripts/run_step.py indicators python scripts/indicators.py
回傳碼與被執行的指令相同（workflow 的 continue-on-error 照常運作）；記錄失敗不影響回傳碼。
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import now_tw, path, read_json, write_json  # noqa: E402

# 步驟 → (輸出檔, 取資料日期的方法)
OUTPUTS = {
    "fetch_prices": ("data/meta.json", lambda j: j.get("tw_latest_date")),
    "market_status": ("data/meta.json", lambda j: (j.get("markets") or {}).get("TWSE", {}).get("data_latest")
                      or (j.get("markets") or {}).get("TW", {}).get("data_latest")),
    "companies": ("data/companies_status.json", lambda j: j.get("generated_at", "")[:10]),
    "revenue": ("data/revenue_status.json", lambda j: j.get("latest_ym")),
    "institutional": ("data/institutional_status.json", lambda j: max((j.get("latest_date") or {}).values(), default=None)),
    "chips": ("data/chips_status.json", lambda j: ((j.get("margin") or {}).get("latest_date") or {}).get("TWSE")),
    "adjust": ("data/adjustments.json", lambda j: j.get("data_date")),
    "indicators": ("data/indicators.json", lambda j: j.get("data_date")),
    "screen": ("data/signals.json", lambda j: j.get("data_date")),
    "paper": ("data/paper.json", lambda j: j.get("data_date")),
}


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    name, cmd = sys.argv[1], sys.argv[2:]
    start = now_tw()
    t0 = time.time()
    rc = subprocess.call(cmd)
    sec = round(time.time() - t0, 1)
    try:
        dd = None
        if name in OUTPUTS:
            p, fn = OUTPUTS[name]
            fp = path(*p.split("/"))
            if os.path.exists(fp):
                with open(fp, encoding="utf-8") as f:
                    dd = fn(json.load(f))
        meta_p = path("data", "meta.json")
        meta = read_json(meta_p, {}) or {}
        pl = meta.get("pipeline") or {}
        if name == "fetch_prices":   # 新的一輪：清掉上一輪的紀錄
            pl = {}
        pl[name] = {"status": "ok" if rc == 0 else ("blocked" if rc == 2 else "failed"), "exit_code": rc,
                    "started_at": start.isoformat(timespec="seconds"),
                    "finished_at": now_tw().isoformat(timespec="seconds"), "seconds": sec, "output_data_date": dd}
        meta["pipeline"] = pl
        write_json(meta_p, meta)
    except Exception as e:  # noqa: BLE001  記錄失敗不影響步驟本身
        print(f"pipeline 紀錄失敗：{e}", file=sys.stderr)
    sys.exit(rc)


if __name__ == "__main__":
    main()
