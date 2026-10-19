# data/ 檔案說明

所有數字由腳本計算；規則參數在 `config/rules.yaml`。每個檔都帶資料日期（欄位或摘要中的 `data_date` / `date` / `ym`）。
本 repo 是公開的：不含任何個人持股或成本。

## 價格

| 檔案 | 產生者 | 內容 |
|---|---|---|
| `prices.csv` | `fetch_prices.py` | 原始日 K（未還原，永不修改）：`code,date,open,high,low,close,volume`。台股全市場保留 `data.keep_calendar_days_all` 天 |
| `adj_factors.csv` | `scripts/adjust.py` | 每個還原事件：`code,date,prev_close,ref_price,factor,volume_factor,reason,source,adj_uncertain`。`adj_uncertain=Y`：還原基準有疑義（例如現金增資除權），選股與回測預設略過 |
| （不存檔） | `scripts/adjlib.py` | **全市場還原價由 `load_adjusted()` 以 prices.csv + adj_factors.csv 即時計算**；指標與回測一律用它。另帶 `adj_uncertain`、`vol_fallback` 旗標 |
| `prices_adj.csv` | `scripts/adjust.py` | 只含關注名單與海外代號的還原價（舊格式，相容既有讀取） |
| `price_fallback_rows.csv` | `fetch_prices.py`、`scripts/backfill.py` | 成交量來自逐檔備援來源（非全市場來源）的列：`code,date,source` |
| `ref_events.csv` | `fetch_prices.py`（經 `scripts/refprice.py`） | 日行情「漲跌」欄反推參考價 ≠ 前收的代號日，以及交易所的除權息標記 |
| `ref_events_coverage.json` | 同上 | 哪些「市場 × 日期」已用漲跌欄檢查過 |
| `events_auto.json` | `scripts/adjust.py` | 證交所 TWT49U、櫃買 exDailyQ 除權息事件快取（全市場） |
| `jump_review.csv` | `scripts/adjust.py` | 單日漲跌超過 10% 的紀錄與判定原因；`needs_confirm=Y` 需人工確認 |
| `adjustments.json` | `scripts/adjust.py` | 還原摘要、待確認清單、事件前收不符清單 |

## 市場狀態（meta.json）

`meta.json` 的 `markets` 區塊（`scripts/market_status.py`）：

```json
"TWSE": {"expected_latest": "2026-10-16", "data_latest": "2026-10-16", "data_matches_expected": true,
         "status": "正常", "reason": "已取得今日資料", "codes_on_latest": 1344, "today_is_session": true,
         "previous_session": "2026-10-15"}
```

`status`：正常 / 休市 / 尚未公布 / 抓取失敗。`data_matches_expected=false` 時，下游不可把舊資料當成最新。
`NYSE` 另有 `session_state`（pre_open / in_session / closed / holiday_or_weekend）與 `stale_codes`；
美股盤中或盤前的 K 棒在 `fetch_prices.py` 已排除（`partial_bars`）。

## 基本面與籌碼

| 檔案 | 產生者 | 更新 | 內容 |
|---|---|---|---|
| `companies.csv` | `scripts/fetch_companies.py` | 每週 | `code,name,market,sec_type,industry_code,industry,full_delivery,shares_outstanding,limit_pct,listed_date,data_date,source` |
| `revenue.csv` | `scripts/fetch_revenue.py` | 每月 1~15 日每天 | `code,market,ym,revenue_k,...,mom_pct,yoy_pct,...,source,source_date`（千元） |
| `institutional.csv` | `scripts/fetch_institutional.py` | 每日 | `date,code,market,unit,foreign_net,...,total_net`（股）＋ `*_lots`（張）＋ `*_value_est`（股數 × 收盤價換算的近似金額）；`code=_MARKET` 為交易所公布的全市場實際金額（unit=TWD） |
| `foreign_holding.csv` | `scripts/fetch_chips.py` | 每日 | `date,code,market,shares_issued,foreign_shares,foreign_pct,foreign_limit_pct,foreign_room_pct` |
| `margin.csv` | `scripts/fetch_chips.py` | 每日 | 融資融券（張）與借券賣出（`sbl_*`，股）餘額與當日增減；`code=_MARKET` 含全市場融資金額 `margin_value_k`（仟元） |
| `tdcc.csv` | `scripts/fetch_chips.py` | 每週 | `date,code,holders,shares,pct_400plus,pct_1000plus,holders_400plus,holders_1000plus`（只能從開始執行起每週累積） |
| `*_status.json` | 各腳本 | — | 來源狀態、最新資料日期 |

## 指標、選股、模擬帳戶（階段 2–4）

| 檔案 | 產生者 | 內容 |
|---|---|---|
| `indicators.json` | `scripts/indicators.py` | `stocks`：全部台股普通股與 ETF 的均線/斜率、ATR、量比、20 日均值、52 週高低、相對 0050、RS 排名、趨勢模板（逐項）、階段、市值、`chips`（法人、外資持股、融資融券、借券、集保與籌碼狀態，各自帶 data_date）、`revenue`；`industries`：產業 5/20 日報酬中位數、RS 中位數、排名；`indicators`：舊版區塊（關注名單 + 海外代號，格式不變）。精簡 JSON（不縮排） |
| `market.json` | `scripts/indicators.py` | 0050 還原收盤、MA20/MA60、季線斜率、單日漲跌與振幅、距 60 日高點回檔、543 階梯、恐慌日追蹤、追蹤日、全市場外資/投信與連續天數、全市場融資與 20 日變化、結論（正常 / 過濾中） |
| `signals.json` | `scripts/screen.py` | 當日 R1/L1/L2/E1/C1 訊號（關鍵數字、參考進場價、停損、每股風險、產業排名、RS 排名、needs_news_check、market_filtered、chips_confirm/chips_warning） |
| `flags.json` | `scripts/screen.py` | 全市場流動股的賣出與警示旗標、market_wide_selloff |
| `paper.json` | `scripts/paper.py` | 規則模擬帳戶：持倉、成交明細、每日淨值與 0050 基準、各規則統計、總報酬、最大回撤 |
| `reports/backtest_YYYYMMDD.md` | `scripts/backtest.py` | 回測報告（手動執行；不提供買賣建議） |

選股、模擬帳戶、回測共用 `scripts/panel.py`（全市場矩陣）與 `scripts/rules.py`（規則），回測與每日選股是同一套邏輯。
`meta.json` 的 `pipeline` 區塊記錄每個步驟的狀態、時間、耗時與輸出檔的資料日期（`scripts/run_step.py`）。
