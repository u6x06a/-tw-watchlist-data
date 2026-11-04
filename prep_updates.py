#!/usr/bin/env python3
import json, os

BEFORE_DIR = "/tmp/claude-0/-home-claude--tw-watchlist-data/d6b1a641-ad57-5ecc-becd-7865d4fd70c3/scratchpad/stocks_before/stocks"
CALC_DIR = "/home/claude/-tw-watchlist-data/calc"
OUT_DIR = "/home/claude/-tw-watchlist-data/writes"
os.makedirs(OUT_DIR, exist_ok=True)

PROTECTED = {"order","name","group","kind","reason","buyPoint","addedAt","addedPrice"}

def load(code):
    with open(os.path.join(BEFORE_DIR, f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)

def load_calc(code):
    with open(os.path.join(CALC_DIR, f"calc_{code}.json"), encoding="utf-8") as f:
        return json.load(f)

def round2(x):
    return round(x, 2) if isinstance(x, float) else x

KNOWN_VERSIONS = {
    "00735": 23, "2303": 12, "3017": 23, "3081": 15,
    "3324": 23, "3653": 24, "6196": 23, "NVDA": 20,
}

def build(code, analysis, newsNote, new_news_item, fund_updates=None, drop_vollots=False):
    before = load(code)
    calc = load_calc(code)
    version = KNOWN_VERSIONS[code]
    # start from calc fields
    doc = dict(calc)
    if drop_vollots:
        doc.pop("volLots", None)
    # round floats
    for k in ("close","prev","chg","chgPct","open","high","low","hi20","lo20","hi60","low5"):
        if k in doc:
            doc[k] = round2(doc[k])
    if "closes" in doc:
        doc["closes"] = [round2(x) for x in doc["closes"]]
    if "ma" in doc:
        doc["ma"] = {k: round2(v) for k,v in doc["ma"].items()}
    if "bias" in doc:
        doc["bias"] = {k: round2(v) for k,v in doc["bias"].items()}

    doc["analysis"] = analysis
    doc["newsNote"] = newsNote

    # news array: prepend new item if given, keep up to 5
    existing_news = before.get("news", [])
    if new_news_item:
        news = [new_news_item] + [n for n in existing_news if n.get("d") != new_news_item["d"]]
    else:
        news = existing_news
    doc["news"] = news[:5]

    # fund
    fund = before.get("fund", [])
    if fund_updates:
        fund = [list(x) for x in fund]
        for key, newval in fund_updates.items():
            found = False
            for row in fund:
                if row[0] == key:
                    row[1] = newval
                    found = True
                    break
            if not found:
                fund.append([key, newval])
    doc["fund"] = fund

    # history: tags as flat text list
    tag_texts = [t["t"] for t in calc.get("tags", [])]
    hist_entry = {
        "date": calc["date"],
        "close": round2(calc["close"]),
        "chgPct": calc["chgPct"],
        "volRatio5": calc["volRatio5"],
        "tags": tag_texts,
        "newsNote": newsNote,
        "news": doc["news"],
        "analysis": analysis,
    }
    history = before.get("history", [])
    history = [h for h in history if h.get("date") != calc["date"]]
    history.append(hist_entry)
    history.sort(key=lambda h: h["date"])
    history = history[-7:]
    doc["history"] = history

    # remove any protected fields that might have leaked in (none should be, since doc built from calc)
    for p in PROTECTED:
        doc.pop(p, None)

    out_path = os.path.join(OUT_DIR, f"{code}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(code, "-> version", version, "date", doc["date"], "close", doc["close"], "chgPct", doc["chgPct"])
    return version

if __name__ == "__main__":
    versions = {}

    versions["3017"] = build(
        "3017",
        analysis="奇鋐收2,820元，重挫3.92%，跌破5日線(3003)、10日線(3106.5)、20日線(3251)，60日線(3236.08)也在上方，均線呈糾結，20日乖離-13.26%。近20日最高3,700元、最低2,670元（當日創新低）；成交量為近5日均量2.5倍爆量。NB與手機零組件族群重挫，奇鋐遭空方狙擊，隨大盤同步承壓。",
        newsNote="11/4 奇鋐遭空方狙擊，股價收2,820元，重挫3.92%，創近20日新低2,670元；NB與手機零組件族群同步重挫（CMoney產業即時新聞）。",
        new_news_item={"d":"11/4","f":False,"src":"CMoney產業即時新聞","text":"電子中游NB與手機零組件族群重挫，奇鋐遭空方狙擊，AI散熱指標股拉回整理，股價收2,820元重挫3.92%，創近20日新低2,670元。"},
    )

    versions["3324"] = build(
        "3324",
        analysis="雙鴻收1,300元，下跌1.52%，盤中一度殺至跌停1,190元（創近20日新低）後收斂跌幅；5日線(1362)、10日線(1371)、20日線(1427.75)仍在上方，並跌破60日線(1322.23)，均線糾結，20日乖離-8.95%。量能為5日均量1.39倍。散熱族群同步承壓。",
        newsNote="11/4 雙鴻盤中一度殺至跌停1,190元（創近20日新低），終場收斂跌幅收1,300元，下跌1.52%，並跌破60日線（鉅亨網盤中速報）。",
        new_news_item={"d":"11/4","f":False,"src":"鉅亨網盤中速報","text":"雙鴻盤中股價殺至跌停1,190元，創近20日新低，成交4,085張；終場收斂跌幅，收1,300元，下跌1.52%，並跌破60日線。"},
    )

    versions["3653"] = build(
        "3653",
        analysis="健策收6,450元，大漲6.88%，站上5日線(6305)、10日線(6301.5)，20日線(6627.25)仍在上方，60日線(5920.33)已在下方，均線糾結，20日乖離-2.67%。量能為5日均量1.79倍放量。近20日最高7,440元、最低5,710元。現價高於買點6095元約5.8%。",
        newsNote="11/4 健策股價大漲6.88%收6,450元，站回5日、10日線並突破買點6095元；未查到更明確的當日個股公告，漲勢與AI伺服器散熱題材延續的市場氛圍一致。",
        new_news_item={"d":"11/4","f":False,"src":"台股盤面","text":"健策股價大漲6.88%收6,450元，站上5日、10日線，突破買點6095元；AI伺服器散熱需求題材延續，帶動買盤回流。"},
    )

    versions["6196"] = build(
        "6196",
        analysis="帆宣收482元，小跌0.92%，5日線(520.9)、10日線(558.55)、20日線(602.27)、60日線(546.49)均在上方，均線糾結，20日乖離-19.97%。量能為5日均量1.57倍放量。近20日最高684元、最低458元（當日創新低）。未查到當日個股消息，股價表現相對抗跌。",
        newsNote="11/4 未查到帆宣當日個股消息，股價收482元，小跌0.92%，創近20日新低458元，相對大盤抗跌。",
        new_news_item={"d":"11/4","f":False,"src":"台股盤面","text":"帆宣股價收482元，小跌0.92%，盤中創近20日新低458元，表現相對抗跌。"},
    )

    versions["2303"] = build(
        "2303",
        analysis="聯電收105.5元，逆勢上漲2.43%，5日線(109.1)、10日線(117.65)、20日線(129.97)、60日線(133.26)均在上方，均線空頭排列，20日乖離-3.3%。近20日最高165元、最低100元（當日觸及）；量縮至5日均量0.81倍。外資今日大舉賣超台股創史上第5大，當日倒貨聯電逾3.35萬張，股價仍逆勢走高。",
        newsNote="11/4 聯電逆勢上漲：外資當日創史上第5大單日賣超台股（約1,499億元）之際，仍大舉賣超聯電逾3.35萬張，但聯電股價逆勢收105.5元，上漲2.43%（自由財經）。",
        new_news_item={"d":"11/4","f":False,"src":"自由財經","text":"外資當日賣超台股創史上第5大（約1,499億元），並倒貨聯電逾3.35萬張，但聯電股價逆勢收105.5元，上漲2.43%。"},
    )

    versions["3081"] = build(
        "3081",
        analysis="聯亞收2,100元，下跌3.45%，盤中一度殺至跌停1,960元（創近20日新低）後收斂跌幅；5日線(2327)、10日線(2420.5)、20日線(2630.5)、60日線(2781.42)均在上方，均線空頭排列，20日乖離-20.17%。近20日最高3,210元、最低1,960元。光通訊族群重災，聯亞一度鎖跌停。",
        newsNote="11/4 聯亞盤中一度殺至跌停1,960元（光通訊族群重災），終場收斂跌幅收2,100元，下跌3.45%（鉅亨網盤中速報）。",
        new_news_item={"d":"11/4","f":False,"src":"鉅亨網盤中速報","text":"光通訊族群重災，聯亞盤中股價殺至跌停1,960元，成交3,908張；終場收斂跌幅，收2,100元，下跌3.45%。"},
    )

    versions["00735"] = build(
        "00735",
        analysis="00735收92.2元，小跌0.59%，5日線(95.88)、10日線(96.8)、20日線(101.06)、60日線(102.77)均在上方，均線空頭排列，20日乖離-8.77%。近20日最高112.85元、最低89.1元（當日創新低）。三星電子收218,000韓元(-0.68%)、SK海力士收1,396,000韓元(-0.14%)，台韓科技股同步走弱。",
        newsNote="11/4 三星電子、SK海力士同步小跌，00735收92.2元，小跌0.59%，創近20日新低89.1元；台幣兌韓元部分資料顯示約40.73（11/4，盤中/部分資料）。",
        new_news_item={"d":"11/4","f":False,"src":"台股盤面","text":"三星電子收218,000韓元（-0.68%）、SK海力士收1,396,000韓元（-0.14%），00735收92.2元，小跌0.59%，創近20日新低89.1元。"},
        fund_updates={
            "三星": "218,000 韓元（11/4收，較11/3 -0.68%）",
            "海力士": "1,396,000 韓元（11/4收，較11/3 -0.14%）",
            "台幣兌韓元": "1 新台幣約 40.73 韓元（11/4，部分/盤中資料，較11/3 -1.24%）",
            "淨值與折溢價": "折溢價 +0.02%（11/3，ETF市價92.75元、淨值92.73元，為最新可查數據）；11/4 ETF市價收92.2元（-0.59%）",
        },
    )

    versions["NVDA"] = build(
        "NVDA",
        analysis="NVDA收206.69美元，上漲2.29%，5日線(208.49)尚未站上（乖離-0.86%），10日線(212.15)、20日線(217.99)、60日線(221.12)均在上方，均線空頭排列。近20日最高239.08美元、最低200.92美元。晶片股同步反彈，帶動NVDA回升。",
        newsNote="11/3（美股最新完整交易日，對應台北11/4傍晚執行時的前一完整美股交易日，不算資料落後）NVDA收206.69美元，上漲2.29%，隨晶片股同步反彈走高。",
        new_news_item={"d":"11/3","f":False,"src":"HeyGoTrade","text":"NVDA股價單日上漲2.3%，隨晶片股同步反彈。"},
        drop_vollots=True,
    )

    with open(os.path.join(OUT_DIR, "_versions.json"), "w") as f:
        json.dump(versions, f, indent=1)
    print(json.dumps(versions, indent=1))
