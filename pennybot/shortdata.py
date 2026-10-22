"""공매도·대차·거래제한 데이터 (모두 무료 공개 자료, 키 불필요).

  - FINRA Reg SHO 일별 공매도 거래량: cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt
      장외(TRF) 보고분 합계라 거래소 전체 공매도는 아니지만, 종목 간·날짜 간 비교에는 충분하다. 미국 동부 18시께 나온다
  - IBKR 대차 가능 수량·수수료: ftp2.interactivebrokers.com (아이디 shortstock, 비밀번호 없음) usa.txt, 15분마다 갱신
      수수료가 높고 가능 수량이 적으면 공매도가 몰려 있다 (숏스퀴즈 재료이면서 하락 베팅이 많다는 뜻)
  - Nasdaq SSR(Rule 201, 전일 −10% → 당일 공매도 호가 제한): nasdaqtrader.com/dynamic/symdir/shorthalts/
  - Nasdaq Reg SHO 결제불이행(threshold) 종목: nasdaqtrader.com/dynamic/symdir/regsho/
  - Nasdaq 거래정지: nasdaqtrader.com/rss.aspx?feed=tradehalts (LULD 변동성 정지, 뉴스 대기 정지 등)
"""
from __future__ import annotations

import csv
import ftplib
import io
import logging
import xml.etree.ElementTree as ET_XML
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

FINRA = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
SSR = "https://www.nasdaqtrader.com/dynamic/symdir/shorthalts/shorthalts{d:%Y%m%d}.txt"
THRESHOLD = "https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{d:%Y%m%d}.txt"
HALTS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
IBKR_HOST, IBKR_USER, IBKR_FILE = "ftp2.interactivebrokers.com", "shortstock", "usa.txt"

# Nasdaq 거래정지 사유 코드 (자주 나오는 것만)
HALT_REASON = {"LUDP": "변동성 정지(LULD)", "LUDS": "변동성 정지(LULD)", "T1": "뉴스 대기", "T2": "뉴스 공개",
               "T3": "뉴스·재개 대기", "T5": "단일 종목 정지", "T6": "비정상 시장 활동", "T8": "ETF 정지",
               "T12": "추가 정보 요청", "H4": "상장 요건 미달", "H9": "공시 지연", "H10": "SEC 거래정지",
               "H11": "규제 문제", "M": "변동성 정지", "MWC1": "시장 서킷브레이커", "D": "상장폐지"}


def _get(url: str, ua: str, timeout: int = 20) -> str | None:
    """없는 날짜 파일은 404 또는 HTML 안내 페이지가 온다 → None."""
    r = requests.get(url, headers={"User-Agent": ua}, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", "replace")
    head = text.lstrip()[:200].lower()
    # 없는 날짜·봇 차단은 HTML 안내 페이지 (XML 피드는 <?xml 로 시작하므로 그대로 둔다)
    return None if head.startswith(("<!doctype html", "<html")) else text


# ---------- 파서 (순수 함수, 테스트 대상) ----------
def parse_finra(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter="|"):
        try:
            d = datetime.strptime(r["Date"], "%Y%m%d").date().isoformat()
            rows.append({"trade_date": d, "symbol": r["Symbol"].strip(), "short_volume": float(r["ShortVolume"]),
                         "exempt_volume": float(r["ShortExemptVolume"] or 0), "total_volume": float(r["TotalVolume"])})
        except (KeyError, ValueError, TypeError):
            continue                                  # 마지막 줄의 합계·빈 줄
    return rows


def parse_ibkr(text: str) -> tuple[str | None, list[dict]]:
    """→ (파일 시각 'YYYY-MM-DDTHH:MM:SS' 미국 동부, 행). AVAILABLE 이 '>10000000' 처럼 올 때도 있다."""
    stamp, rows = None, []
    for line in text.splitlines():
        if line.startswith("#BOF"):
            p = line.split("|")
            if len(p) >= 3:
                stamp = f"{p[1].replace('.', '-')}T{p[2]}"
            continue
        if line.startswith("#"):
            continue
        p = line.split("|")
        if len(p) < 8:
            continue
        sym = p[0].strip()
        if not sym or not sym.replace(".", "").isalpha():
            continue                                  # 채권 CUSIP 등
        try:
            avail = float(p[7].lstrip(">").strip() or 0)
            rows.append({"symbol": sym, "rebate_rate": float(p[5] or 0), "fee_rate": float(p[6] or 0),
                         "available": avail})
        except ValueError:
            continue
    return stamp, rows


def parse_ssr(text: str, day: date) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        sym = (r.get("Symbol") or "").strip()
        if sym:
            rows.append({"trade_date": day.isoformat(), "symbol": sym, "trigger_time": (r.get("Trigger Time") or "").strip()})
    return rows


def parse_threshold(text: str, day: date) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter="|"):
        sym = (r.get("Symbol") or "").strip()
        if sym and (r.get("Reg SHO Threshold Flag") or "").strip() == "Y":
            rows.append({"trade_date": day.isoformat(), "symbol": sym})
    return rows


def parse_halts(text: str) -> list[dict]:
    ns = {"ndaq": "http://www.nasdaqtrader.com/"}
    root = ET_XML.fromstring(text.lstrip("﻿").encode())
    rows = []
    for it in root.iter("item"):
        g = lambda k: (it.findtext(f"ndaq:{k}", default="", namespaces=ns) or "").strip()
        sym, hd, ht = g("IssueSymbol"), g("HaltDate"), g("HaltTime")
        if not (sym and hd and ht):
            continue
        try:
            halt = datetime.strptime(f"{hd} {ht[:8]}", "%m/%d/%Y %H:%M:%S").replace(tzinfo=NY)
        except ValueError:
            continue
        resume = None
        rd, rt = g("ResumptionDate"), g("ResumptionTradeTime")
        if rd and rt:
            try:
                resume = datetime.strptime(f"{rd} {rt[:8]}", "%m/%d/%Y %H:%M:%S").replace(tzinfo=NY).isoformat()
            except ValueError:
                pass
        code = g("ReasonCode")
        rows.append({"symbol": sym, "halt_ts": halt.isoformat(), "reason": code,
                     "reason_ko": HALT_REASON.get(code, code), "resume_ts": resume})
    return rows


# ---------- 받기 ----------
class ShortData:
    def __init__(self, user_agent: str = "pennybot/1.0"):
        self.ua = user_agent

    def recent_days(self, now: datetime, n: int = 5) -> list[date]:
        """미국 동부 기준 오늘부터 거꾸로 평일 n개 (휴장일 파일은 없으므로 None 으로 걸러진다)."""
        d, out = now.astimezone(NY).date(), []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d -= timedelta(days=1)
        return out

    def finra(self, day: date) -> list[dict]:
        t = _get(FINRA.format(d=day), self.ua)
        return parse_finra(t) if t else []

    def ssr(self, day: date) -> list[dict]:
        t = _get(SSR.format(d=day), self.ua)
        return parse_ssr(t, day) if t else []

    def threshold(self, day: date) -> list[dict]:
        t = _get(THRESHOLD.format(d=day), self.ua)
        return parse_threshold(t, day) if t else []

    def halts(self) -> list[dict]:
        t = _get(HALTS, self.ua)
        return parse_halts(t) if t else []

    def borrow(self) -> tuple[str | None, list[dict]]:
        f = ftplib.FTP(IBKR_HOST, timeout=30)
        try:
            f.login(IBKR_USER, "")
            buf = io.BytesIO()
            f.retrbinary(f"RETR {IBKR_FILE}", buf.write)
        finally:
            try:
                f.quit()
            except Exception:
                f.close()
        return parse_ibkr(buf.getvalue().decode("utf-8", "replace"))
