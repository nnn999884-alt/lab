"""희석(증자) 분석: 공시 본문을 읽어 '희석 이벤트'를 방향과 함께 판정하고, 회사별 증자 여력을 추정한다.

페니 급등주의 핵심 변수는 차트가 아니라 회사가 지금 주식을 팔 수 있느냐다.
  - 악재(−): 새 ATM(시장가 분할 매각), 등록 직접 공모(RD), 공모, 사모(PIPE), 증권매매계약(SPA),
            주식 매수 약정(ELOC), 워런트 행사가 인하/행사 유도, 전환사채, 증자 등록(S-3) 및 효력 발생
  - 호재(+): ATM 종료, 증자 등록 철회(RW), 자사주 매입, 내부자 장내매수, 상장 요건 회복,
            증자와 무관한 중요 계약(8-K 1.01)
  - 주의: 8-K 1.01 은 페니주에서 대부분 증자 계약이다 → 본문에 증권매매계약·공모 문구가 있으면 악재로 본다.

규칙은 키워드 기반이고 영어 공시 문구를 전제로 한다. 판정 이유(근거 문장 일부)를 함께 남겨 검증할 수 있게 했다.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .screener import edgar as ed

log = logging.getLogger(__name__)

# (이벤트, 방향, 정규식) — 위에서부터 검사, 한 공시에 여러 이벤트가 붙을 수 있다
_W = r"[\s\S]{0,250}?"
RULES: list[tuple[str, int, re.Pattern]] = [
    ("ATM_TERMINATED", +1, re.compile(
        rf"terminat\w*{_W}(at[- ]the[- ]market|sales agreement|equity distribution agreement|atm (program|facility|offering))"
        rf"|(at[- ]the[- ]market|sales agreement|equity distribution agreement){_W}terminat", re.I)),
    ("OFFERING_TERMINATED", +1, re.compile(
        r"(terminat|withdr[ae]w|abandon)\w*\s+(the\s+)?(proposed\s+)?(public\s+)?offering", re.I)),
    ("BUYBACK", +1, re.compile(r"(share|stock) repurchase (program|plan)|buyback program", re.I)),
    ("COMPLIANCE_REGAINED", +1, re.compile(r"regain(ed|s)? compliance", re.I)),
    ("ATM_NEW", -1, re.compile(r"at[- ]the[- ]market (offering|issuance|program|sales)|equity distribution agreement"
                               r"|sales agreement with", re.I)),
    ("REGISTERED_DIRECT", -1, re.compile(r"registered direct", re.I)),
    ("PUBLIC_OFFERING", -1, re.compile(r"(underwritten|best[- ]efforts|firm commitment)\s+(public\s+)?offering"
                                       r"|public offering of \S+ (shares|units)", re.I)),
    ("PRIVATE_PLACEMENT", -1, re.compile(r"private placement|private investment in public equity", re.I)),
    ("SECURITIES_PURCHASE_AGREEMENT", -1, re.compile(r"securities purchase agreement|placement agency agreement", re.I)),
    ("EQUITY_LINE", -1, re.compile(r"equity line|common stock purchase agreement|committed equity facility", re.I)),
    ("WARRANT_INDUCEMENT", -1, re.compile(r"inducement{_W}warrant|warrant{_W}inducement".replace("{_W}", _W), re.I)),
    ("WARRANT_REPRICE", -1, re.compile(r"(reduc|lower|amend)\w*{_W}exercise price".replace("{_W}", _W), re.I)),
    ("CONVERTIBLE", -1, re.compile(r"convertible (promissory )?(note|debenture)s?", re.I)),
    ("REVERSE_SPLIT", -1, re.compile(r"reverse (stock )?split", re.I)),
]
FINANCING = {"ATM_NEW", "REGISTERED_DIRECT", "PUBLIC_OFFERING", "PRIVATE_PLACEMENT",
             "SECURITIES_PURCHASE_AGREEMENT", "EQUITY_LINE", "WARRANT_INDUCEMENT"}
# 본문 없이 양식만으로 판정
FORM_EVENTS = {
    "RW": ("REGISTRATION_WITHDRAWN", +1), "S-3": ("SHELF_FILED", -1), "F-3": ("SHELF_FILED", -1),
    "S-1": ("OFFERING_FILED", -1), "F-1": ("OFFERING_FILED", -1), "EFFECT": ("REGISTRATION_EFFECTIVE", -1),
    "424B4": ("PUBLIC_OFFERING", -1), "424B1": ("PUBLIC_OFFERING", -1),
}
TEXT_FORMS = ("8-K", "6-K", "424B5", "424B3", "424B2", "424B7", "RW", "S-3", "S-1")
_AMOUNT = re.compile(r"(?:aggregate offering price|gross proceeds|up to)[^$]{0,80}\$\s?([\d,.]+)\s*(million|billion)?", re.I)


def clean_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


def parse_amount(text: str) -> float | None:
    """공모 금액(달러). 여러 개면 가장 큰 값."""
    vals = []
    for num, unit in _AMOUNT.findall(text):
        try:
            v = float(num.replace(",", "").rstrip("."))
        except ValueError:
            continue
        v *= {"million": 1e6, "billion": 1e9}.get((unit or "").lower(), 1)
        if 1e4 <= v <= 5e9:
            vals.append(v)
    return max(vals) if vals else None


@dataclass
class Event:
    kind: str
    direction: int          # +1 호재 / −1 악재
    form: str
    filed: date
    accession: str
    amount: float | None = None
    evidence: str = ""


def classify(form: str, items: str, text: str, filed: date, accession: str = "") -> list[Event]:
    form = form.upper()
    out: list[Event] = []
    if form in FORM_EVENTS:
        k, d = FORM_EVENTS[form]
        out.append(Event(k, d, form, filed, accession))
    t = clean_text(text) if text else ""
    item_set = {i.strip() for i in (items or "").split(",") if i.strip()}
    if "3.01" in item_set:
        out.append(Event("DELISTING_NOTICE", -1, form, filed, accession))
    if "3.02" in item_set:
        out.append(Event("PRIVATE_PLACEMENT", -1, form, filed, accession))
    found = set()
    current_report = form.startswith(("8-K", "6-K"))
    for kind, direction, rx in RULES:
        # 호재 문구(종료·철회·자사주)는 수시 공시에서만 믿는다.
        # 증자 설명서에는 "이 공모는 ~하면 종료된다" 같은 상투 문구가 항상 들어 있다.
        if direction > 0 and not current_report:
            continue
        m = next((m for m in rx.finditer(t)
                  if not (kind == "ATM_TERMINATED" and re.search(r"will terminate|terminate upon|may terminate",
                                                                  m.group(0), re.I))), None)
        if not m:
            continue
        # ATM 종료 문서에는 'sales agreement' 가 함께 나오므로 신규 ATM 으로 중복 판정하지 않는다
        if kind == "ATM_NEW" and "ATM_TERMINATED" in found:
            continue
        found.add(kind)
        s = max(0, m.start() - 60)
        out.append(Event(kind, direction, form, filed, accession, evidence=t[s:m.end() + 60].strip()))
    if form.startswith("424B5") and "ATM_NEW" not in found and "REGISTERED_DIRECT" not in found and t:
        out.append(Event("TAKEDOWN", -1, form, filed, accession))   # 증자 등록에서 실제로 꺼내 쓰는 공모
    if "1.01" in item_set and not (found & (FINANCING | {"ATM_TERMINATED", "CONVERTIBLE"})):
        out.append(Event("MATERIAL_CONTRACT", +1, form, filed, accession))
    if any(e.direction < 0 and e.kind in FINANCING | {"TAKEDOWN", "PUBLIC_OFFERING"} for e in out):
        amt = parse_amount(t)
        for e in out:
            if e.direction < 0:
                e.amount = amt
    return out


# ---------- 재무 (XBRL companyfacts): 현금 소진 시점 · 워런트 ----------
CASH_TAGS = [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
             ("us-gaap", "Cash"), ("ifrs-full", "CashAndCashEquivalents")]
OPCF_TAGS = [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
             ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")]
WARRANT_TAGS = [("us-gaap", "ClassOfWarrantOrRightOutstanding")]
WARRANT_PRICE_TAGS = [("us-gaap", "ClassOfWarrantOrRightExercisePriceOfWarrantsOrRights")]


def _entries(facts: dict, tags, today: date) -> list[dict]:
    """as-of today 기준으로 이미 제출된 값만 (백테스트에서 미래 재무제표를 보지 않게)."""
    out = []
    for ns, tag in tags:
        units = ((facts.get("facts") or {}).get(ns) or {}).get(tag, {}).get("units", {})
        for vals in units.values():
            for v in vals:
                filed = v.get("filed")
                if filed and date.fromisoformat(filed) <= today and v.get("val") is not None:
                    out.append(v)
        if out:
            break
    return out


def financials(facts: dict, today: date) -> dict:
    cash = max(_entries(facts, CASH_TAGS, today), key=lambda v: v["end"], default=None)
    burn = None
    flows = [v for v in _entries(facts, OPCF_TAGS, today) if v.get("start")]
    if flows:
        v = max(flows, key=lambda v: (v["end"], v["start"]))
        months = (date.fromisoformat(v["end"]) - date.fromisoformat(v["start"])).days / 30.44
        if months >= 2.5:
            burn = max(-float(v["val"]) / months, 0.0)     # 월 현금 소진 (양수 = 쓰는 중)
    out = {"cash": float(cash["val"]) if cash else None, "cash_date": cash["end"] if cash else None,
           "burn_month": burn, "runway_months": None,
           "warrants": None, "warrant_price": None}
    if cash and burn:
        elapsed = (today - date.fromisoformat(cash["end"])).days / 30.44
        out["runway_months"] = max(float(cash["val"]) - burn * elapsed, 0.0) / burn
    w = max(_entries(facts, WARRANT_TAGS, today), key=lambda v: v["end"], default=None)
    wp = max(_entries(facts, WARRANT_PRICE_TAGS, today), key=lambda v: v["end"], default=None)
    out["warrants"] = float(w["val"]) if w else None
    out["warrant_price"] = float(wp["val"]) if wp else None
    return out


@dataclass
class Profile:
    """회사의 증자 여력·이력 요약. score 0~100: 높을수록 지금 주식을 팔 가능성이 크다."""
    score: float = 20.0
    bucket: str = "unknown"
    atm_active: bool = False
    shelf_active: bool = False
    baby_shelf: bool = False
    capacity_usd: float | None = None      # 남은 증자 한도 추정 (baby shelf 일 때)
    raised_12m: float = 0.0
    days_since_financing: int | None = None
    recent_positive: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    runway_months: float | None = None
    warrants: float | None = None
    warrant_price: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_profile(events: list[Event], today: date, market_cap: float | None, fin: dict | None = None,
                  price: float | None = None, shares: float | None = None) -> Profile:
    p = Profile()
    evs = sorted(events, key=lambda e: e.filed)
    shelf_at = atm_at = None
    for e in evs:
        if e.kind in ("SHELF_FILED", "REGISTRATION_EFFECTIVE") and e.form in ("S-3", "F-3", "EFFECT"):
            shelf_at = e.filed
        elif e.kind == "REGISTRATION_WITHDRAWN":
            shelf_at = None
        elif e.kind == "ATM_NEW":
            atm_at = e.filed
        elif e.kind == "ATM_TERMINATED":
            atm_at = None
    p.shelf_active = bool(shelf_at and (today - shelf_at).days <= 3 * 365)
    p.atm_active = bool(atm_at)
    raises = [e for e in evs if e.direction < 0 and (e.kind in FINANCING or e.kind in ("TAKEDOWN", "PUBLIC_OFFERING"))]
    if raises:
        p.days_since_financing = (today - raises[-1].filed).days
    p.raised_12m = sum(e.amount or 0 for e in raises if (today - e.filed).days <= 365)
    if market_cap is not None:
        # 유통 시가총액 7,500만 달러 미만이면 S-3 로 12개월에 유통주식 가치의 1/3 까지만 팔 수 있다 (baby shelf)
        p.baby_shelf = market_cap < 75e6
        if p.baby_shelf and p.shelf_active:
            p.capacity_usd = max(market_cap / 3 - p.raised_12m, 0.0)

    s = 20.0
    if p.atm_active:
        s += 35; p.reasons.append("ATM 진행 중")
    if p.shelf_active:
        if p.capacity_usd is not None and market_cap and p.capacity_usd < market_cap * 0.1:
            s += 5; p.reasons.append(f"증자 등록 있으나 남은 한도 적음(~${p.capacity_usd / 1e6:.1f}M)")
        else:
            s += 20; p.reasons.append("증자 등록(S-3) 유효")
    if p.days_since_financing is not None and p.days_since_financing <= 30:
        s += 25; p.reasons.append(f"최근 {p.days_since_financing}일 내 증자")
    cutoff = lambda d: [e for e in evs if (today - e.filed).days <= d]
    kinds_180 = {e.kind for e in cutoff(180)}
    if kinds_180 & {"CONVERTIBLE", "EQUITY_LINE"}:
        s += 10; p.reasons.append("전환사채/주식 매수 약정")
    if {e.kind for e in cutoff(90)} & {"WARRANT_INDUCEMENT", "WARRANT_REPRICE"}:
        s += 10; p.reasons.append("워런트 행사가 인하/행사 유도")
    if "REVERSE_SPLIT" in kinds_180:
        s += 10; p.reasons.append("최근 액면병합")
    if "OFFERING_FILED" in {e.kind for e in cutoff(120)}:
        s += 15; p.reasons.append("공모 신고서(S-1/F-1) 제출")
    if fin:
        p.runway_months, p.warrants, p.warrant_price = fin.get("runway_months"), fin.get("warrants"), \
            fin.get("warrant_price")
        r = p.runway_months
        if r is not None:
            # 현금이 곧 바닥나는 회사는 주가가 오르면 바로 판다
            if r < 3:
                s += 20; p.reasons.append(f"현금 {r:.1f}개월치 (증자 임박)")
            elif r < 6:
                s += 15; p.reasons.append(f"현금 {r:.1f}개월치")
            elif r < 12:
                s += 7; p.reasons.append(f"현금 {r:.0f}개월치")
        if p.warrants and shares and p.warrants / shares >= 0.2 and \
                (p.warrant_price is None or not price or p.warrant_price <= price * 1.3):
            itm = " (내가격)" if p.warrant_price and price and p.warrant_price <= price else ""
            s += 10; p.reasons.append(f"행사 가능 워런트 발행주식의 {p.warrants / shares * 100:.0f}%{itm}")
    pos = [e for e in cutoff(30) if e.direction > 0]
    if any(e.kind == "ATM_TERMINATED" for e in pos):
        s -= 30
    if any(e.kind in ("REGISTRATION_WITHDRAWN", "OFFERING_TERMINATED") for e in pos):
        s -= 20
    if any(e.kind == "BUYBACK" for e in pos):
        s -= 10
    p.recent_positive = sorted({e.kind for e in pos})
    p.score = max(0.0, min(100.0, s))
    p.bucket = "low" if p.score < 35 else "high" if p.score >= 60 else "mid"
    return p


class DilutionService:
    """EDGAR 에서 공시를 받아 판정하고 디스크에 캐시한다 (공시 본문은 바뀌지 않으므로 영구 캐시)."""

    def __init__(self, edgar: ed.EdgarClient, cache_dir: str | Path = "state/edgar_cache",
                 lookback_days: int = 3 * 365, max_texts: int = 40):
        self.edgar = edgar
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lookback = lookback_days
        self.max_texts = max_texts
        self._subs: dict[int, tuple[date, dict]] = {}
        self._fin: dict[tuple[int, date], dict | None] = {}

    def submissions(self, cik: int, today: date) -> dict:
        hit = self._subs.get(cik)
        if hit and hit[0] == today:
            return hit[1]
        j = self.edgar.submissions(cik)
        self._subs[cik] = (today, j)
        return j

    def _events_for(self, cik: int, f: ed.Filing) -> list[Event]:
        path = self.dir / f"{f.accession}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw = raw["events"] if isinstance(raw, dict) else raw      # 예전 캐시는 이벤트 목록만
            return [Event(**{**e, "filed": date.fromisoformat(e["filed"])}) for e in raw]
        text, ex10 = "", []
        if f.form.upper().startswith(TEXT_FORMS) and f.primary_doc:
            try:
                text = self.edgar.document(cik, f)
            except Exception as e:
                log.debug("공시 본문 실패 %s: %s", f.accession, e)
                return classify(f.form, f.items, "", f.filed, f.accession)   # 캐시하지 않고 다음에 재시도
            if f.form.upper().startswith(("8-K", "6-K")):
                text, ex10 = self._with_exhibits(cik, f, text)
        evs = classify(f.form, f.items, text, f.filed, f.accession)
        # 계약서(EX-10)는 해지 조항 같은 상투 문구가 많아 악재 판정에만 쓴다
        have = {e.kind for e in evs}
        for t in ex10:
            for e in classify(f.form, "", t, f.filed, f.accession):
                if e.direction < 0 and e.kind not in have:
                    evs.append(e)
                    have.add(e.kind)
        from .filing_detail import extract
        detail = extract(f.form, f.items, clean_text(text) if text else "",
                         max((e.amount or 0 for e in evs), default=0) or None)
        path.write_text(json.dumps({"events": [{**asdict(e), "filed": e.filed.isoformat()} for e in evs], "detail": detail},
                                   ensure_ascii=False), encoding="utf-8")
        return evs

    def detail_for(self, accession: str) -> dict | None:
        """_events_for 가 본문에서 뽑아 둔 세부 숫자 (가격·주식 수·워런트·주관사 등). 예전 캐시면 None."""
        path = self.dir / f"{accession}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw.get("detail") if isinstance(raw, dict) else None

    def _with_exhibits(self, cik: int, f: ed.Filing, text: str) -> tuple[str, list[str]]:
        """8-K/6-K 첨부: 보도자료(EX-99)는 본문에 붙여 그대로 판정, 계약서(EX-10)는 따로 돌려준다."""
        try:
            docs = self.edgar.exhibits(cik, f)
        except Exception as e:
            log.debug("첨부 목록 실패 %s: %s", f.accession, e)
            return text, []
        ex99 = [u for t, u in docs if t.upper().startswith("EX-99")][:2]
        ex10 = [u for t, u in docs if t.upper().startswith("EX-10")][:2]
        fetch = lambda u: clean_text(self.edgar.url_text(u))[:40000]
        extra, contracts = [], []
        for u in ex99:
            try:
                extra.append(fetch(u))
            except Exception as e:
                log.debug("첨부 실패 %s: %s", u, e)
        for u in ex10:
            try:
                contracts.append(fetch(u))
            except Exception as e:
                log.debug("첨부 실패 %s: %s", u, e)
        return text + " " + " ".join(extra), contracts

    def events(self, cik: int, today: date) -> list[Event]:
        filings = ed.parse_submissions(self.submissions(cik, today))
        recent = [f for f in filings if 0 <= (today - f.filed).days <= self.lookback]   # 미래 공시 제외
        # 증자 등록·효력·철회는 본문 없이 양식만으로 판정되므로 개수 제한 없이 모두 본다.
        # (공시가 많은 회사는 최근 40건 안에 3년 전 S-3 가 안 들어올 수 있다)
        out, texts = [], 0
        for f in recent:                          # 최신순
            form = f.form.upper()
            if form in FORM_EVENTS and form not in ("RW", "S-3", "S-1"):
                out += classify(f.form, f.items, "", f.filed, f.accession)
            elif form.startswith(TEXT_FORMS):
                if texts < self.max_texts:
                    out += self._events_for(cik, f)
                    texts += 1
                elif form in FORM_EVENTS:
                    out += classify(f.form, f.items, "", f.filed, f.accession)
        return out

    def financials(self, cik: int, today: date) -> dict | None:
        key = (cik, today)
        if key not in self._fin:
            try:
                self._fin[key] = financials(self.edgar.companyfacts(cik), today)
            except Exception as e:      # 재무 XBRL 이 없는 회사도 많다 (신규 상장, 일부 해외 발행사)
                log.debug("재무 데이터 없음 %s: %s", cik, e)
                self._fin[key] = None
        return self._fin[key]

    def profile(self, cik: int, today: date, market_cap: float | None, price: float | None = None,
                shares: float | None = None) -> Profile:
        return build_profile(self.events(cik, today), today, market_cap, self.financials(cik, today),
                             price, shares)

    def company(self, cik: int, today: date) -> dict:
        j = self.submissions(cik, today)
        biz = (j.get("addresses") or {}).get("business") or {}
        return {"name": j.get("name"), "country": biz.get("stateOrCountry"),
                "country_desc": biz.get("stateOrCountryDescription"),
                "incorporation": j.get("stateOfIncorporation")}


# 미국 주 코드 (EDGAR stateOrCountry). 이 밖의 코드는 해외 (예: F4 중국, K3 홍콩, U0 싱가포르, E9 케이맨)
US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
                "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())


def pump_flags(*, list_date: date | None, country: str | None, shares_outstanding: float | None,
               today: date, has_catalyst: bool) -> list[str]:
    """FINRA 공지 22-25 의 위험 신호를 우리가 볼 수 있는 데이터로 근사. 3개 이상이면 펌프 의심."""
    flags = []
    if list_date and (today - list_date) <= timedelta(days=365):
        flags.append("상장 1년 이내")
    if country and country.upper() not in US_STATES:
        flags.append(f"해외 발행사({country})")
    if shares_outstanding is not None and shares_outstanding < 15e6:
        flags.append("발행주식 1,500만 주 미만")
    if not has_catalyst:
        flags.append("공시·촉매 없는 급등")
    return flags
