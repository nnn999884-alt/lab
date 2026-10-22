"""보도자료(뉴스와이어) 수집: 페니주 촉매와 급락 원인은 SEC 공시보다 보도자료가 먼저인 경우가 많다.

무료 RSS/Atom 피드를 읽어 본문의 '(NASDAQ: ABCD)' 같은 표기로 종목을 찾고,
dilution.py 와 같은 규칙 + 뉴스 전용 규칙으로 호재/악재를 판정한다.
  악재: 공모 가격 결정(pricing), 공모 제안, 상장 요건 미달 통지, 액면병합, 계속기업 불확실성
  호재: FDA 승인·허가, 계약·수주·파트너십, ATM 종료, 자사주 매입, 상장 요건 회복
피드 주소는 바뀔 수 있어 설정(collector.news_feeds)으로 둔다. 실패한 피드는 건너뛴다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .dilution import Event, classify, clean_text

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA==",
    "https://www.accesswire.com/rss/latest",
]

_TICKER = re.compile(r"\(?\b(NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE MKT|NYSE|AMEX|NasdaqCM|NasdaqGM|NasdaqGS)"
                     r"\s*[:：]\s*\"?([A-Z]{1,5}(?:\.[A-Z])?)\b")

NEWS_RULES: list[tuple[str, int, re.Pattern]] = [
    ("PUBLIC_OFFERING", -1, re.compile(r"announces? (the )?pricing of|prices? \$?[\d.,]+ (million )?(underwritten |public |registered direct )?offering"
                                       r"|priced (an? )?(underwritten |public |registered direct )?offering", re.I)),
    ("OFFERING_FILED", -1, re.compile(r"(proposed|launch(es)?) (an? )?(underwritten |public )?offering", re.I)),
    ("DELISTING_NOTICE", -1, re.compile(r"(notice|notification|letter) from (the )?(nasdaq|nyse)|delisting determination"
                                        r"|minimum bid price (requirement|deficiency)", re.I)),
    ("GOING_CONCERN", -1, re.compile(r"going concern", re.I)),
    ("FDA_APPROVAL", +1, re.compile(r"\bFDA\b[\s\S]{0,60}(approv|clear|grant|authoriz)", re.I)),
    ("MATERIAL_NEWS", +1, re.compile(r"(award(ed|s)?|secur(es|ed)|receiv(es|ed)|win(s)?) [\s\S]{0,60}(contract|purchase order|order from)"
                                     r"|(strategic |definitive )?(partnership|collaboration|supply|distribution|license) agreement", re.I)),
]


@dataclass
class Item:
    id: str
    title: str
    url: str
    summary: str
    published: datetime


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)          # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))   # Atom: ISO 8601
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str) -> list[Item]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        pub = _dt(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if pub and title:
            out.append(Item(guid, title, link, clean_text(it.findtext("description") or ""), pub))
    for e in root.iter(f"{atom}entry"):
        title = (e.findtext(f"{atom}title") or "").strip()
        ln = e.find(f"{atom}link")
        link = ln.get("href", "") if ln is not None else ""
        pub = _dt(e.findtext(f"{atom}updated") or e.findtext(f"{atom}published"))
        body = e.findtext(f"{atom}summary") or e.findtext(f"{atom}content") or ""
        if pub and title:
            out.append(Item((e.findtext(f"{atom}id") or link or title).strip(), title, link, clean_text(body), pub))
    return out


def tickers(text: str) -> set[str]:
    return {m.group(2) for m in _TICKER.finditer(text)}


def classify_news(title: str, summary: str, published: date) -> list[Event]:
    text = f"{title}. {summary}"
    # 보도자료는 수시 공시와 같은 성격 → 호재 규칙도 적용 (8-K 로 보고 판정)
    evs = [e for e in classify("8-K", "", text, published) if e.kind not in ("MATERIAL_CONTRACT",)]
    have = {e.kind for e in evs}
    for kind, direction, rx in NEWS_RULES:
        if kind in have:
            continue
        m = rx.search(text)
        if m:
            s = max(0, m.start() - 60)
            evs.append(Event(kind, direction, "NEWS", published, "", evidence=text[s:m.end() + 60]))
            have.add(kind)
    # 공모 뉴스 안의 'agreement' 같은 단어로 호재가 같이 잡히면 악재만 남긴다
    if any(e.direction < 0 and e.kind in ("PUBLIC_OFFERING", "OFFERING_FILED", "REGISTERED_DIRECT",
                                          "SECURITIES_PURCHASE_AGREEMENT", "ATM_NEW") for e in evs):
        evs = [e for e in evs if e.direction < 0]
    return evs


def item_id(source: str, it: Item) -> str:
    return hashlib.sha1(f"{source}|{it.id}".encode()).hexdigest()[:20]


class NewsClient:
    def __init__(self, feeds: list[str] | None = None, user_agent: str = "pennybot/1.0"):
        self.feeds = feeds or DEFAULT_FEEDS
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent})

    def fetch(self) -> list[tuple[str, Item]]:
        out = []
        for url in self.feeds:
            try:
                r = self.s.get(url, timeout=15)
                r.raise_for_status()
                src = re.sub(r"^https?://(www\.|feed\.)?", "", url).split("/")[0]
                out += [(src, it) for it in parse_feed(r.text)]
            except Exception as e:
                log.debug("뉴스 피드 실패 %s: %s", url, e)
        return out
