"""공시에서 판단에 필요한 숫자만 뽑는다 (본문 영어 문구 기준, 정규식).

  공모·사모·RD : 총액, 주당 가격, 주식 수, 워런트(수량·행사가), 사전 납입 워런트 여부, 주관사·배정 대행사, 종결 예정일
  ATM          : 한도, 판매 대행사
  상폐 통지    : 미달 요건(최저호가·자기자본·시총·공시 지연), 회복 기한
  액면병합     : 비율, 효력일
  보도자료     : 'announced ...' 첫 문장
  Form 4       : 누가(직책) 무엇을(매수·매도·행사) 몇 주를 얼마에
못 찾은 값은 넣지 않는다 (틀린 숫자보다 빈칸이 낫다).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

ITEMS_KO = {"1.01": "중요 계약 체결", "1.02": "중요 계약 해지", "1.03": "파산·회생", "2.01": "자산 인수·처분",
            "2.02": "실적 발표", "2.03": "채무 발생", "2.04": "채무 가속", "2.05": "구조조정", "2.06": "자산 손상",
            "3.01": "상장 요건 미달 통지", "3.02": "미등록 주식 발행(사모)", "3.03": "주주 권리 변경",
            "4.01": "감사인 교체", "4.02": "과거 재무제표 신뢰 불가", "5.01": "지배권 변경", "5.02": "임원·이사 변경",
            "5.03": "정관 변경(액면병합 등)", "5.07": "주총 결과", "5.08": "주총 일정", "7.01": "공정공시(IR 자료)",
            "8.01": "기타 중요 사항", "9.01": "첨부 서류"}

FORM_KO = {"8-K": "수시공시", "8-K/A": "수시공시 정정", "6-K": "해외 수시공시", "4": "내부자 거래", "424B1": "공모 확정",
           "424B3": "증권신고서", "424B4": "공모 확정", "424B5": "증권 발행", "S-1": "공모 신고", "F-1": "공모 신고",
           "S-3": "증자 등록", "F-3": "증자 등록", "RW": "등록 철회", "EFFECT": "등록 효력", "NEWS": "보도자료"}
EVENT_KO = {"ATM_TERMINATED": "ATM 종료", "OFFERING_TERMINATED": "공모 철회", "REGISTRATION_WITHDRAWN": "증자 등록 철회",
            "BUYBACK": "자사주 매입", "COMPLIANCE_REGAINED": "상장 요건 회복", "MATERIAL_CONTRACT": "중요 계약",
            "ATM_NEW": "ATM 신규", "REGISTERED_DIRECT": "등록 직접 공모", "PUBLIC_OFFERING": "공모", "PRIVATE_PLACEMENT": "사모",
            "SECURITIES_PURCHASE_AGREEMENT": "증권매매계약", "EQUITY_LINE": "주식 매수 약정", "WARRANT_INDUCEMENT": "워런트 행사 유도",
            "WARRANT_REPRICE": "워런트 행사가 인하", "CONVERTIBLE": "전환사채", "REVERSE_SPLIT": "액면병합", "TAKEDOWN": "증자 실행",
            "SHELF_FILED": "증자 등록(S-3)", "OFFERING_FILED": "공모 신고", "REGISTRATION_EFFECTIVE": "등록 효력 발생",
            "DELISTING_NOTICE": "상폐 통지", "FDA_APPROVAL": "FDA 승인", "MATERIAL_NEWS": "계약·수주", "GOING_CONCERN": "계속기업 불확실"}

# 페니주 증자에 자주 나오는 주관사·배정 대행사 (이름이 보이면 그대로 적는다)
AGENTS = ["H.C. Wainwright", "Maxim Group", "Aegis Capital", "ThinkEquity", "EF Hutton", "Spartan Capital", "Roth Capital",
          "A.G.P.", "Alliance Global Partners", "Dawson James", "Univest", "Craig-Hallum", "Ladenburg Thalmann",
          "Joseph Gunnar", "Boustead", "D. Boral", "Kingswood", "R.F. Lafferty", "Titan Partners", "WestPark",
          "Rodman & Renshaw", "Benchmark", "Laidlaw", "Network 1", "Prime Number", "Lucid Capital", "Clear Street",
          "Cantor Fitzgerald", "B. Riley", "JonesTrading", "Canaccord", "Oppenheimer", "Brookline", "Chardan",
          "Newbridge", "Wedbush", "Northland", "Lake Street", "Ascendiant", "Revere Securities", "Bancroft",
          "Yorkville", "Lincoln Park", "Univest Securities", "American Trust Investment", "Paulson Investment"]

_NUM = r"([\d,]+(?:\.\d+)?)"
_MILLION = r"(?:\s*(million|billion))?"
_DATE = r"((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2},? \d{4})"
_RX = {
    "price": re.compile(r"(?:public offering|offering|purchase|combined|sale) price (?:of|is|was|equal to)[^$]{0,40}\$\s?" + _NUM
                        + r"\s*per (?:share|unit)", re.I),
    "price2": re.compile(r"at (?:a |an )?(?:price|purchase price) of \$\s?" + _NUM + r"\s*per (?:share|unit)", re.I),
    "shares": re.compile(r"(?:aggregate of |up to |sale of |offering of |issue and sell |sell )" + _NUM + _MILLION
                         + r"\s*(?:shares|units)\b", re.I),
    "warrants": re.compile(r"warrants? to purchase (?:up to |an aggregate of |up to an aggregate of )?" + _NUM + _MILLION
                           + r"\s*shares[\s\S]{0,200}?exercise price of \$\s?" + _NUM, re.I),
    "ex_price": re.compile(r"exercise price of \$\s?" + _NUM + r"\s*per share", re.I),
    "atm": re.compile(r"(?:aggregate (?:offering|sales) price|having an aggregate[^$]{0,30}) of up to \$\s?" + _NUM + _MILLION, re.I),
    "closing": re.compile(r"(?:expected to close|closing (?:is expected|of the offering is expected)) (?:on or about )?" + _DATE, re.I),
    "split": re.compile(r"\b(1|one)[- ]for[- ](\d+|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|twenty-five|thirty|forty|fifty|one hundred)\b", re.I),
    "effective": re.compile(r"(?:become |be |is )?effective[^;]{0,80}?" + _DATE, re.I),   # "12:01 a.m." 의 마침표 허용
    "until": re.compile(r"(?:until|through|by) " + _DATE + r"[^.]{0,80}?(?:regain|compliance)|(?:regain compliance)[^.]{0,120}?(?:until|by) " + _DATE, re.I),
    "announced": re.compile(r"(?:today announced|announced today|announced)(?: that)? ([^.]{25,220}\.)", re.I),
}
_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
          "twenty": 20, "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50, "one hundred": 100}


def _num(s: str, unit: str | None = None) -> float | None:
    try:
        v = float(s.replace(",", "").rstrip("."))
    except (ValueError, AttributeError):
        return None
    return v * {"million": 1e6, "billion": 1e9}.get((unit or "").lower(), 1)


def extract(form: str, items: str, text: str, amount: float | None = None) -> dict:
    """text 는 clean_text() 를 거친 본문 (+ 보도자료 첨부)."""
    d: dict = {}
    item_list = [i.strip() for i in (items or "").split(",") if i.strip() and i.strip() != "9.01"]
    if item_list:
        d["items"] = [f"{i} {ITEMS_KO.get(i, '')}".strip() for i in item_list]
    if not text:
        return d
    t = text[:200000]
    m = _RX["price"].search(t) or _RX["price2"].search(t)
    if m:
        d["price"] = _num(m.group(1))
    m = _RX["shares"].search(t)
    if m:
        v = _num(m.group(1), m.group(2))
        if v and v >= 1000:
            d["shares"] = v
    m = _RX["warrants"].search(t)
    if m:
        d["warrants"] = _num(m.group(1), m.group(2))
        d["warrant_price"] = _num(m.group(3))
    elif (m := _RX["ex_price"].search(t)) and re.search(r"\bwarrants?\b", t, re.I):
        d["warrant_price"] = _num(m.group(1))
    if re.search(r"pre-funded warrants?", t, re.I):
        d["prefunded"] = True
    m = _RX["atm"].search(t)
    if m and re.search(r"at[- ]the[- ]market|sales agreement|equity distribution", t, re.I):
        d["atm_size"] = _num(m.group(1), m.group(2))
    if amount:
        d["gross"] = amount
    agents = [a for a in AGENTS if a.lower() in t.lower()]
    if agents:
        d["agent"] = agents[0]
    m = _RX["closing"].search(t)
    if m:
        d["closing"] = m.group(1)
    if "3.01" in (items or "") or re.search(r"deficiency|not in compliance|delist", t, re.I):
        why = []
        for rx, ko in ((r"minimum bid price", "최저호가 $1 미달"), (r"stockholders[’'] equity", "자기자본 미달"),
                       (r"market value of listed securities", "상장 시가총액 미달"),
                       (r"market value of publicly held shares", "유통주식 시가총액 미달"),
                       (r"(?:timely|late) fil|form 10-[qk]", "정기공시 지연"), (r"audit committee", "감사위원회 요건")):
            if re.search(rx, t, re.I):
                why.append(ko)
        if why and ("3.01" in (items or "")):
            d["deficiency"] = why[:2]
            m = _RX["until"].search(t)
            if m:
                d["deadline"] = m.group(1) or m.group(2)
    m = _RX["split"].search(t)
    if m and re.search(r"reverse (stock )?split", t, re.I):
        n = m.group(2).lower()
        d["split"] = f"1:{_WORDS.get(n, n)}"
        e = _RX["effective"].search(t[m.start():m.start() + 1500])
        if e:
            d["split_effective"] = e.group(1)
    m = _RX["announced"].search(t)
    if m and form.upper().startswith(("8-K", "6-K")):
        d["headline"] = re.sub(r"\s+", " ", m.group(1)).strip()[:220]
    return d


# ---------- Form 4 ----------
CODE_KO = {"P": "장내 매수", "S": "장내 매도", "A": "보상 지급", "M": "옵션·워런트 행사", "F": "세금 납부용 매도",
           "G": "증여", "C": "전환", "X": "옵션 행사", "D": "회사에 매도"}


def form4(xml_text: str) -> dict:
    """→ {'owner': 이름, 'role': 직책, 'tx': [{'code','ko','shares','price','value'}], 'net_buy_usd'}"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    owner = root.findtext("reportingOwner/reportingOwnerId/rptOwnerName", "").strip()
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    role = ""
    if rel is not None:
        title = (rel.findtext("officerTitle") or "").strip()
        if title:
            role = title
        elif (rel.findtext("isDirector") or "").strip() in ("1", "true"):
            role = "이사"
        elif (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true"):
            role = "10% 대주주"
    txs, net = [], 0.0
    for tx in root.iter("nonDerivativeTransaction"):
        code = tx.findtext("transactionCoding/transactionCode", "").strip()
        try:
            sh = float(tx.findtext("transactionAmounts/transactionShares/value", "0") or 0)
            px = float(tx.findtext("transactionAmounts/transactionPricePerShare/value", "0") or 0)
        except ValueError:
            continue
        ad = tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value", "").strip()
        txs.append({"code": code, "ko": CODE_KO.get(code, code), "shares": sh, "price": px or None, "value": sh * px or None})
        if code == "P" and ad == "A":
            net += sh * px
        elif code == "S" and ad == "D":
            net -= sh * px
    return {"owner": owner.title() if owner.isupper() else owner, "role": role, "tx": txs[:6], "net_buy_usd": net}


def summary(form: str, d: dict, market_price: float | None = None) -> str:
    """사람이 읽는 한 줄 (텔레그램·대시보드 공용). 없는 값은 건너뛴다."""
    if not d:
        return ""
    usd = lambda v: f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K" if v >= 1e3 else f"${v:,.0f}"
    qty = lambda v: f"{v / 1e6:.1f}M주" if v >= 1e6 else f"{v / 1e3:.0f}K주" if v >= 1e3 else f"{v:,.0f}주"
    px = lambda v: f"${v:.4f}" if v < 1 else f"${v:.2f}"
    if form == "4":
        parts = []
        who = " ".join(x for x in (d.get("role"), d.get("owner")) if x)
        for tx in d.get("tx", [])[:3]:
            s = f"{tx['ko']} {qty(tx['shares'])}" if tx.get("shares") else tx["ko"]
            if tx.get("price"):
                s += f" @ {px(tx['price'])}"
            if tx.get("value"):
                s += f" ({usd(tx['value'])})"
            parts.append(s)
        return (who + ": " if who else "") + ", ".join(parts)
    parts = []
    if d.get("gross"):
        parts.append(usd(d["gross"]))
    if d.get("atm_size"):
        parts.append(f"ATM 한도 {usd(d['atm_size'])}")
    if d.get("price"):
        s = f"주당 {px(d['price'])}"
        if market_price:
            s += f" (현재가 대비 {(d['price'] / market_price - 1) * 100:+.0f}%)"
        parts.append(s)
    if d.get("shares"):
        parts.append(qty(d["shares"]))
    if d.get("warrants") or d.get("warrant_price"):
        w = "워런트"
        if d.get("warrants"):
            w += f" {qty(d['warrants'])}"
        if d.get("warrant_price"):
            w += f" @ {px(d['warrant_price'])}"
        parts.append(w)
    if d.get("prefunded"):
        parts.append("사전납입 워런트 포함")
    if d.get("agent"):
        parts.append(d["agent"])
    if d.get("closing"):
        parts.append(f"종결 {d['closing']}")
    if d.get("deficiency"):
        parts.append(" · ".join(d["deficiency"]) + (f" → 기한 {d['deadline']}" if d.get("deadline") else ""))
    if d.get("split"):
        parts.append(f"액면병합 {d['split']}" + (f" ({d['split_effective']} 효력)" if d.get("split_effective") else ""))
    return " · ".join(parts)
