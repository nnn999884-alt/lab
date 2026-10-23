"""주가를 올려야 할 이유 (부양 동기): 회사가 지금 주가가 높기를 바라는 사정.

페니주 급등은 회사 사정과 겹칠 때가 많다. 예: 상장 유지 기한이 다가오는 회사, 증자를 앞둔 회사(ATM·공모 대기),
행사가가 현재가 근처인 워런트를 가진 회사(주가가 행사가를 넘으면 현금이 들어온다), 현금이 바닥나는 회사,
정기보고서를 곧 내야 하는 회사(실적·사업 보도자료를 몰아 내는 시기).
같은 사정이 '급등 뒤 바로 증자' 로 이어지기도 하므로 호재·악재가 아니라 '움직일 이유' 로 본다.
판단은 모두 as-of 날짜 기준 (그날까지 제출된 공시와 그날까지의 가격만) → 과거 검증에도 그대로 쓴다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

# Nasdaq 상장 규정 5810: 최저 호가 미달 통지 후 180일 안에 10거래일 연속 $1 이상이면 회복 (한 번 더 180일 연장 가능)
COMPLIANCE_DAYS = 180
PERIODIC = {"10-Q": 45, "10-K": 90, "20-F": 120}   # 소규모 공시회사의 기간 말일 → 제출 기한 (일)


@dataclass
class Motive:
    key: str          # delisting / below_1 / atm / offering / warrants / cash / reverse_split / meeting / periodic
    label: str        # 짧은 이름 (표·배지)
    detail: str       # 한 줄 설명
    days: int | None = None   # 기한까지 남은 날 (있을 때)

    def to_dict(self) -> dict:
        return asdict(self)


def _below_streak(closes: list[tuple[date, float]] | None, today: date) -> int:
    """오늘까지 종가 $1 미만이 이어진 거래일 수."""
    n = 0
    for d, c in sorted((x for x in closes or [] if x[0] <= today), reverse=True):
        if c >= 1.0:
            break
        n += 1
    return n


def motives(events: list, filings: list, fin: dict | None, today: date, price: float | None,
            closes: list[tuple[date, float]] | None = None) -> list[Motive]:
    """events: dilution.Event 목록, filings: edgar.Filing 목록, fin: dilution.financials() 결과."""
    evs = sorted((e for e in events if e.filed <= today), key=lambda e: e.filed)
    out: list[Motive] = []

    # 1) 상장 유지: 상폐 통지(8-K 3.01) 뒤 회복 공시가 없으면 기한까지 주가를 $1 위로 올려야 한다
    notice = next((e for e in reversed(evs) if e.kind == "DELISTING_NOTICE" and (today - e.filed).days <= 2 * COMPLIANCE_DAYS),
                  None)
    if notice and not any(e.kind == "COMPLIANCE_REGAINED" and e.filed >= notice.filed for e in evs):
        deadline = notice.filed + timedelta(days=COMPLIANCE_DAYS)
        if deadline < today:
            deadline += timedelta(days=COMPLIANCE_DAYS)        # 보통 한 번 연장된다
        left = (deadline - today).days
        out.append(Motive("delisting", f"상폐 기한 D-{left}",
                          f"{notice.filed} 상장 요건 미달 통지 → {deadline} 까지 10거래일 연속 $1 이상 필요", left))
    streak = _below_streak(closes, today)
    if streak >= 20:
        out.append(Motive("below_1", f"$1 미만 {streak}일",
                          "30거래일 연속 $1 미만이면 상폐 통지 대상" if streak < 30 and not notice
                          else f"종가 $1 미만 {streak}거래일째 (액면병합·부양 압력)"))

    # 2) 증자 준비: ATM 진행 중이면 주가가 높을수록 같은 주식으로 더 많이 판다
    atm = None
    for e in evs:
        if e.kind == "ATM_NEW":
            atm = e
        elif e.kind == "ATM_TERMINATED":
            atm = None
    if atm:
        out.append(Motive("atm", "ATM 진행 중", f"{atm.filed} ATM(시장가 분할 매도) 계약 → 오르면 바로 팔 수 있음"))
    ready = [e for e in evs if e.kind in ("OFFERING_FILED", "REGISTRATION_EFFECTIVE") and (today - e.filed).days <= 120]
    took = [e for e in evs if e.kind in ("TAKEDOWN", "PUBLIC_OFFERING", "REGISTERED_DIRECT") and ready
            and e.filed >= ready[-1].filed]
    if ready and not took:
        e = ready[-1]
        what = "공모 신고서(S-1/F-1) 제출" if e.kind == "OFFERING_FILED" else "증자 등록 효력 발생"
        out.append(Motive("offering", "공모 대기", f"{e.filed} {what} → 아직 실제 발행 공시 없음"))

    # 3) 워런트: 주가가 행사가를 넘으면 행사 → 회사에 현금 (행사가 근처에서 부양 유인)
    if fin and fin.get("warrants") and fin.get("warrant_price") and price:
        wp = fin["warrant_price"]
        gap = (wp / price - 1) * 100
        if -50 <= gap <= 100:
            out.append(Motive("warrants", f"워런트 ${wp:g}",
                              f"미행사 워런트 {fin['warrants'] / 1e6:.1f}M주, 행사가 ${wp:g} (현재가 대비 {gap:+.0f}%)"))

    # 4) 현금: 곧 바닥나면 증자가 필요하고, 주가가 높아야 덜 희석된다
    r = (fin or {}).get("runway_months")
    if r is not None and r < 6:
        # 마지막 재무제표 뒤로 소진 속도대로 빼므로 0 이면 '이미 바닥났을 것' (그사이 조달했을 수도)
        label = "현금 바닥 추정" if r < 0.5 else f"현금 {r:.1f}개월"
        out.append(Motive("cash", label, f"마지막 재무제표·현금 소진 속도로 추정한 남은 현금 {r:.1f}개월치 → 자금 조달 필요"))

    # 5) 액면병합·주총
    rs = next((e for e in reversed(evs) if e.kind == "REVERSE_SPLIT" and (today - e.filed).days <= 120), None)
    if rs:
        out.append(Motive("reverse_split", "액면병합", f"{rs.filed} 액면병합 관련 공시"))
    proxy = next((f for f in filings if f.form in ("PRE 14A", "DEF 14A", "PRE 14C", "DEF 14C")
                  and 0 <= (today - f.filed).days <= 60), None)
    if proxy:
        out.append(Motive("meeting", "주총 예정", f"{proxy.filed} {proxy.form} 주총 안건 공시 (액면병합·증자 한도 안건 흔함)"))

    # 6) 정기보고서 기한: 마지막 보고서의 기간 말일 + 3개월(분기) / 1년(연간) → 다음 기한
    due = _next_periodic_due(filings, today)
    if due and 0 <= (due[1] - today).days <= 21:
        left = (due[1] - today).days
        out.append(Motive("periodic", f"{due[0]} D-{left}", f"{due[0]} 제출 기한 {due[1]} (실적·사업 보도자료가 몰리는 시기)", left))
    return out


_TICKERS: dict[str, int] = {}
_LIVE: dict[tuple[str, date], dict] = {}


def live(dil, symbol: str, price: float | None, today: date | None = None,
         closes: list[tuple[date, float]] | None = None) -> dict | None:
    """지금 기준 희석 점수 + 부양 동기 (종목 조회·/종목 용). 하루 한 번만 계산해 둔다. EDGAR 없으면 None."""
    from .dilution import build_profile
    from .screener import edgar as ed
    if dil is None:
        return None
    today = today or date.today()
    key = (symbol.upper(), today)
    if key in _LIVE:
        return _LIVE[key]
    if not _TICKERS:
        _TICKERS.update({str(r["ticker"]).upper(): int(r["cik"]) for r in dil.edgar.ticker_map()})
    cik = _TICKERS.get(symbol.upper())
    if not cik:
        return None
    evs = dil.events(cik, today)
    fin = dil.financials(cik, today)
    filings = ed.parse_submissions(dil.submissions(cik, today))
    prof = build_profile(evs, today, None, fin, price, None)
    _LIVE[key] = {"dilution_score": prof.score, "dilution_reasons": prof.reasons,
                  "motives": [m.to_dict() for m in motives(evs, filings, fin, today, price, closes)]}
    return _LIVE[key]


def daily_closes(research_db: str, symbol: str) -> list[tuple[date, float]] | None:
    """과거 검증이 받아 둔 일봉이 있으면 ($1 미만 지속 판단용)."""
    import sqlite3
    from pathlib import Path
    if not Path(research_db).exists():
        return None
    con = sqlite3.connect(Path(research_db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT d, close FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT 80", (symbol.upper(),)).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return [(date.fromisoformat(d), c) for d, c in rows] or None


def _next_periodic_due(filings: list, today: date) -> tuple[str, date] | None:
    periodic = [f for f in filings if f.form in PERIODIC and f.report_date and f.filed <= today]
    if not periodic:
        return None
    last = max(periodic, key=lambda f: f.report_date)
    end = date.fromisoformat(last.report_date)
    # 다음 기간: 분기 보고서 3번 뒤에는 연간 보고서
    q_after_k = sum(1 for f in periodic if f.form == "10-Q" and f.report_date > max(
        (x.report_date for x in periodic if x.form in ("10-K", "20-F")), default=""))
    if last.form == "20-F":
        nxt, form = end.replace(year=end.year + 1), "20-F"
    elif last.form == "10-Q" and q_after_k >= 3:
        nxt, form = _add_months(end, 3), "10-K"
    else:
        nxt, form = _add_months(end, 3), "10-Q"
    return (form, nxt + timedelta(days=PERIODIC[form]))


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)
