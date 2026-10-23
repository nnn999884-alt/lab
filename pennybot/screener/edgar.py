"""SEC EDGAR 공시 기반 '촉매 / 희석 방패' 레이어 (무료 공개 데이터).

페니주 손실의 가장 큰 원인은 차트가 아니라 '희석(유상증자)'과 '상장폐지'다.
대부분의 스캐너는 이걸 안 본다. 여기서는 공시로:
  - 차단: 최근 증자 공시(424B/S-1/S-3/F-1/F-3), 상폐 통지(8-K 3.01), 정기보고 지연(NT 10-K/Q)
  - 감점: 사모 증자(8-K 3.02), 정관 변경=액면병합 가능성(8-K 5.03)
  - 가점: 내부자 장내 매수(Form 4, 거래코드 P), 5% 이상 지분 취득(SC 13D), 중요 계약(8-K 1.01)

SEC 는 User-Agent 에 연락처를 요구한다 → 환경변수 SEC_USER_AGENT="이름 이메일".
요청 한도 초당 10회.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

import requests

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"   # 재무제표 XBRL 값
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
# 최신 접수 공시 Atom 피드. <updated> 에 접수 시각이 시간대와 함께 들어 있다 (세션 판정용)
CURRENT_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}"
               "&company=&dateb=&owner=include&start=0&count=100&output=atom")

OFFERING_FORMS = ("424B", "S-1", "S-3", "F-1", "F-3")
LATE_FILING_FORMS = ("NT 10-K", "NT 10-Q", "NT 20-F")


@dataclass
class CatalystReport:
    score: float = 0.0
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)

    def add(self, pts: float, why: str) -> None:
        self.score += pts
        self.reasons.append(f"{why}({pts:+g})")

    def block(self, why: str) -> None:
        self.blocked = True
        self.reasons.append(f"차단:{why}")


@dataclass
class Filing:
    form: str
    filed: date
    accession: str
    primary_doc: str
    items: str = ""
    accepted: str = ""   # submissions API 의 acceptanceDateTime (원문 그대로)
    report_date: str = ""   # 정기보고서(10-Q/10-K)의 대상 기간 말일


class EdgarClient:
    def __init__(self, user_agent: str, min_interval: float = 0.12):
        if not user_agent:
            raise ValueError("SEC_USER_AGENT 환경변수(예: 'pennybot 홍길동 me@example.com')가 필요합니다.")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._lock = threading.Lock()
        self._last = 0.0
        self._min = min_interval

    def _get(self, url: str) -> requests.Response:
        with self._lock:
            wait = self._min - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        r = self.s.get(url, timeout=15)
        r.raise_for_status()
        return r

    def ticker_map(self) -> list[dict]:
        """[{cik, name, ticker, exchange}] — OTC 제외는 호출 측에서."""
        j = self._get(TICKERS_URL).json()
        f = j["fields"]
        return [dict(zip(f, row)) for row in j["data"]]

    def submissions(self, cik: int) -> dict:
        """회사 정보(주소·설립지 등) + 최근 공시 목록 원본."""
        return self._get(SUBMISSIONS_URL.format(cik=cik)).json()

    def companyfacts(self, cik: int) -> dict:
        return self._get(FACTS_URL.format(cik=cik)).json()

    def filings(self, cik: int) -> list[Filing]:
        return parse_submissions(self.submissions(cik))

    def exhibits(self, cik: int, f: Filing) -> list[tuple[str, str]]:
        """공시 색인 페이지에서 첨부 문서 목록 → [(유형, URL)] 예: ('EX-99.1', 'https://...')."""
        acc = f.accession.replace("-", "")
        html = self._get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{f.accession}-index.htm").text
        return parse_index(html)

    def url_text(self, url: str) -> str:
        return self._get(url).text

    def document(self, cik: int, f: Filing) -> str:
        """공시 본문(주 문서) 원문."""
        return self._get(ARCHIVE_URL.format(cik=cik, acc=f.accession.replace("-", ""),
                                            doc=f.primary_doc.split("/")[-1])).text

    def current(self, form: str) -> list[dict]:
        return parse_current_atom(self._get(CURRENT_URL.format(form=form)).text)

    def form4_xml(self, cik: int, f: Filing) -> str:
        # primaryDocument 는 'xslF345X05/form4.xml' 처럼 렌더링 경로 → 원본 XML 은 접두 제거
        doc = f.primary_doc.split("/")[-1]
        return self._get(ARCHIVE_URL.format(cik=cik, acc=f.accession.replace("-", ""), doc=doc)).text


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
_HREF = re.compile(r'href="([^"]+)"', re.I)


def parse_index(html: str) -> list[tuple[str, str]]:
    """EDGAR '-index.htm' 의 문서 표 (순번·설명·문서·유형·크기)에서 (유형, 절대 URL)."""
    out = []
    for row in _ROW.findall(html):
        cells = _CELL.findall(row)
        if len(cells) < 4:
            continue
        href = _HREF.search(cells[2])
        typ = re.sub(r"<[^>]+>", "", cells[3]).strip()
        if not href or not typ:
            continue
        url = href.group(1).replace("/ix?doc=", "")
        out.append((typ, url if url.startswith("http") else "https://www.sec.gov" + url))
    return out


def parse_submissions(j: dict) -> list[Filing]:
    r = j.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(r.get("form", [])):
        out.append(Filing(
            form=form,
            filed=date.fromisoformat(r["filingDate"][i]),
            accession=r["accessionNumber"][i],
            primary_doc=r.get("primaryDocument", [""] * (i + 1))[i],
            items=(r.get("items") or [""] * (i + 1))[i] or "",
            accepted=(r.get("acceptanceDateTime") or [""] * (i + 1))[i] or "",
            report_date=(r.get("reportDate") or [""] * (i + 1))[i] or "",
        ))
    return out


_ATOM = "{http://www.w3.org/2005/Atom}"
_TITLE = re.compile(r"^(?P<form>.+?) - .*\((?P<cik>\d{10})\) \((?P<role>[A-Za-z ]+)\)\s*$")


def parse_current_atom(xml_text: str) -> list[dict]:
    """최신 공시 피드 → [{form, cik, role, accession, accepted(datetime, 시간대 포함)}].

    Form 4 는 같은 공시가 Issuer(회사)와 Reporting(내부자) 두 줄로 나온다 → 회사 쪽만 쓰려면 role 로 거른다.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for e in root.iter(f"{_ATOM}entry"):
        m = _TITLE.match((e.findtext(f"{_ATOM}title") or "").strip())
        acc = re.search(r"accession-number=([\d-]+)", e.findtext(f"{_ATOM}id") or "")
        upd = e.findtext(f"{_ATOM}updated")
        if not (m and acc and upd):
            continue
        out.append({"form": m["form"].strip(), "cik": int(m["cik"]), "role": m["role"].strip(),
                    "accession": acc.group(1), "accepted": datetime.fromisoformat(upd)})
    return out


def insider_buy_usd(xml_text: str) -> float:
    """Form 4 XML 에서 장내매수(P) 금액 합계."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return 0.0
    total = 0.0
    for tx in root.iter("nonDerivativeTransaction"):
        code = tx.findtext("transactionCoding/transactionCode", "").strip()
        ad = tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value", "").strip()
        if code != "P" or ad != "A":
            continue
        try:
            sh = float(tx.findtext("transactionAmounts/transactionShares/value", "0"))
            px = float(tx.findtext("transactionAmounts/transactionPricePerShare/value", "0"))
        except ValueError:
            continue
        total += sh * px
    return total


def evaluate(filings: list[Filing], today: date, form4_buys_usd: float = 0.0,
             cfg: dict | None = None) -> CatalystReport:
    """공시 목록 → 촉매 점수. 순수 함수 (테스트 가능)."""
    c = {"offering_block_days": 30, "offering_penalty_days": 90, "lookback_days": 120,
         "insider_window_days": 30, **(cfg or {})}
    rep = CatalystReport()
    seen: set[str] = set()

    def once(key: str) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    for f in filings:
        age = (today - f.filed).days
        if age < 0 or age > c["lookback_days"]:
            continue
        form = f.form.upper()
        if form.startswith(OFFERING_FORMS):
            if age <= c["offering_block_days"]:
                rep.block(f"{form} 증자공시 {age}일 전")
            elif age <= c["offering_penalty_days"] and once("offer"):
                rep.add(-25, f"{form} 증자공시")
        elif form in LATE_FILING_FORMS and age <= 90:
            rep.block(f"{form} 보고서 지연")
        elif form in ("SC 13D", "SC 13D/A", "SCHEDULE 13D") and age <= 60 and once("13d"):
            rep.add(8, "13D 지분취득")
        elif form == "8-K":
            items = {x.strip() for x in f.items.split(",") if x.strip()}
            if "3.01" in items and age <= 90:
                rep.block("상장폐지 통지(8-K 3.01)")
            if "3.02" in items and age <= 90 and once("302"):
                rep.add(-15, "사모증자(8-K 3.02)")
            if "5.03" in items and once("503"):
                rep.add(-10, "정관변경/액면병합(8-K 5.03)")
            if "1.01" in items and age <= 30 and once("101"):
                rep.add(3, "중요계약(8-K 1.01)")

    if form4_buys_usd >= 10_000:
        # 금액이 클수록 가점, 상한 20점
        pts = min(20.0, 8 + 4 * (form4_buys_usd / 50_000))
        rep.add(round(pts, 1), f"내부자 장내매수 ${form4_buys_usd:,.0f}")
    return rep


def recent_form4(filings: list[Filing], today: date, days: int = 30, limit: int = 8) -> list[Filing]:
    return [f for f in filings if f.form == "4" and 0 <= (today - f.filed).days <= days][:limit]
