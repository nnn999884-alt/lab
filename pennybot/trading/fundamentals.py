"""종목별 희석 프로필 · 펌프 위험 신호 · 발행주식 정보 (하루 단위 캐시).

SEC_USER_AGENT 가 없으면 EDGAR 를 못 쓰므로 희석 프로필은 None 이 되고, 전략은 수집기 DB 의 공시만으로 판단한다.
"""
from __future__ import annotations

import logging
from datetime import date

from ..dilution import DilutionService, Profile, pump_flags

log = logging.getLogger(__name__)


class Fundamentals:
    def __init__(self, client, dilution: DilutionService | None = None):
        self.c = client
        self.dil = dilution
        self._cik: dict[str, int] | None = None
        self._stock: dict[tuple[str, date], dict] = {}
        self._profile: dict[tuple[str, date], Profile | None] = {}
        self._company: dict[tuple[str, date], dict] = {}

    def cik(self, symbol: str) -> int | None:
        if self.dil is None:
            return None
        if self._cik is None:
            try:
                self._cik = {str(r["ticker"]).upper(): int(r["cik"]) for r in self.dil.edgar.ticker_map()}
            except Exception as e:
                log.warning("SEC 티커 목록 실패: %s", e)
                return None
        return self._cik.get(symbol.upper())

    def stock(self, symbol: str, today: date) -> dict:
        key = (symbol, today)
        if key not in self._stock:
            try:
                rows = self.c.stocks([symbol]) or []
                self._stock[key] = rows[0] if rows else {}
            except Exception as e:
                log.debug("종목 정보 실패 %s: %s", symbol, e)
                self._stock[key] = {}
        return self._stock[key]

    def shares(self, symbol: str, today: date) -> float | None:
        v = self.stock(symbol, today).get("sharesOutstanding")
        return float(v) if v else None

    def profile(self, symbol: str, price: float, today: date) -> Profile | None:
        key = (symbol, today)
        if key not in self._profile:
            cik = self.cik(symbol)
            sh = self.shares(symbol, today)
            try:
                self._profile[key] = self.dil.profile(cik, today, sh * price if sh else None) if cik else None
            except Exception as e:
                log.warning("희석 프로필 실패 %s: %s", symbol, e)
                self._profile[key] = None
        return self._profile[key]

    def flags(self, symbol: str, today: date, has_catalyst: bool) -> list[str]:
        st = self.stock(symbol, today)
        ld = st.get("listDate")
        country = None
        cik = self.cik(symbol)
        if cik:
            key = (symbol, today)
            if key not in self._company:
                try:
                    self._company[key] = self.dil.company(cik, today)
                except Exception:
                    self._company[key] = {}
            country = self._company[key].get("country")
        return pump_flags(list_date=date.fromisoformat(ld) if ld else None, country=country,
                          shares_outstanding=self.shares(symbol, today), today=today, has_catalyst=has_catalyst)
