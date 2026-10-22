"""세 전략: 데이마켓에 사서 그날 밤(프리마켓~정규장 개장 직후)에 판다.

모든 숫자는 백테스트 전 초안이다(config.trading.strategies). 전략은 '후보와 계획'만 만든다.
주문·수량·리스크는 trader / risk 가 맡는다.

  opening_premium : 직전 영업일 주목받은 종목 → 데이마켓 매수 → 프리 후반~개장 직후 매도
                    근거: 주목 종목은 밤사이 오르고 개장 후 되밀린다 (Berkman 외 2012)
  filing_reaction : 장 마감 후 내부자 장내매수 / 호재 8-K → 데이마켓 매수 → 프리마켓~개장 매도
                    가설: 데이마켓은 미국 공시를 덜 반영하고, 미국 투자자가 돌아오는 프리마켓에 반영된다
  day_dip         : 데이마켓에서 공시 없이 급락한 종목 → 하락이 멈추면 매수 → 프리마켓 매도
                    근거: 유동성이 귀할 때 공급하는 대가 (단기 반전)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..screener import edgar as ed
from ..sessions import DAY, PRE, REGULAR, SessionCalendar

POSITIVE_8K = {"1.01"}                 # 중요 계약 체결
NEGATIVE_8K = {"3.01", "3.02", "5.03"}  # 상폐 통지, 사모 증자, 정관 변경(액면병합)


@dataclass
class Plan:
    strategy: str
    symbol: str
    ok: bool
    reason: str
    score: float = 0.0
    price: float = 0.0          # 매수 지정가
    stop: float = 0.0
    target: float = 0.0
    sell_from: datetime | None = None
    deadline: datetime | None = None


@dataclass
class Ctx:
    now: datetime
    trade_date: date
    session: str
    cal: SessionCalendar
    mv: object                       # MarketView
    tcfg: object                     # TradingConfig
    held: set = field(default_factory=set)

    def bound(self, session: str):
        return self.cal.bounds(self.trade_date, session)

    @property
    def regular_open(self) -> datetime:
        return self.bound(REGULAR)[0]

    @property
    def deadline(self) -> datetime:
        return self.regular_open + timedelta(minutes=self.tcfg.forced_exit_minutes)


def shared_filter(sym: str, price: float, ctx: Ctx) -> str | None:
    """모든 전략 공통: 가격대 · 희석/상폐 공시 · 군중 쏠림."""
    t = ctx.tcfg
    if not (t.min_price <= price <= t.max_price):
        return f"가격범위 밖 ${price:.4f}"
    filings = [ed.Filing(f["form"], datetime.fromisoformat(f["accepted"]).date(), "", "", f["items"] or "")
               for f in ctx.mv.filings_for(sym, 120, ctx.now)]
    rep = ed.evaluate(filings, ctx.trade_date)
    if rep.blocked:
        return "희석/상폐 공시: " + "; ".join(r for r in rep.reasons if r.startswith("차단"))
    r5 = ctx.mv.ret_5d(sym, ctx.trade_date)
    if r5 is not None and r5 > 0.5:
        return f"5일 +{r5 * 100:.0f}% (군중 쏠림)"
    return None


def quote_check(sym: str, ctx: Ctx):
    b = ctx.mv.book(sym)
    if not b.has_quote:
        return b, "데이마켓 호가 없음"
    if b.spread_pct > ctx.tcfg.max_spread_pct:
        return b, f"스프레드 {b.spread_pct:.1f}%"
    return b, None


class Strategy:
    name = ""

    def __init__(self, params: dict):
        self.p = params

    def scan(self, ctx: Ctx) -> list[Plan]:
        raise NotImplementedError

    def _reject(self, sym, why, price=0.0) -> Plan:
        return Plan(self.name, sym, False, why, price=price)


class OpeningPremium(Strategy):
    name = "opening_premium"

    def scan(self, ctx: Ctx) -> list[Plan]:
        prev = ctx.mv.prev_trade_date(ctx.trade_date)
        if not prev:
            return []
        out = []
        for sym, rank in ctx.mv.attention(prev):
            if sym in ctx.held:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date)
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, bad, b.last))
                continue
            why = shared_filter(sym, b.mid, ctx)
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            chg = (b.mid / pc - 1) * 100 if pc else 0.0
            if not (self.p["min_change_pct"] <= chg <= self.p["max_change_pct"]):
                out.append(self._reject(sym, f"전일 종가 대비 {chg:+.1f}% (범위 밖)", b.mid))
                continue
            # 스프레드가 좁으면 ask 로 바로 사고, 넓으면 중간가 근처에 걸어 비용을 줄인다
            price = b.ask if b.spread_pct <= 1.0 else min(b.ask, b.mid * 1.002)
            out.append(Plan(self.name, sym, True, f"전일 랭킹 {rank}위, 데이마켓 {chg:+.1f}%",
                            score=100 - rank, price=price,
                            stop=price * (1 - self.p["stop_loss_pct"] / 100),
                            target=price * (1 + self.p["take_profit_pct"] / 100),
                            sell_from=ctx.regular_open - timedelta(minutes=self.p["sell_before_open_minutes"]),
                            deadline=ctx.deadline))
        return out


class FilingReaction(Strategy):
    name = "filing_reaction"

    def scan(self, ctx: Ctx) -> list[Plan]:
        prev = ctx.mv.prev_trade_date(ctx.trade_date)
        prev_reg = ctx.cal.bounds(prev, REGULAR) if prev else None
        since = prev_reg[1] if prev_reg else ctx.now - timedelta(hours=18)
        by_sym: dict[str, list[dict]] = {}
        for f in ctx.mv.filings_since(since):
            if f["symbol"]:
                by_sym.setdefault(f["symbol"], []).append(f)
        out = []
        for sym, fs in by_sym.items():
            if sym in ctx.held:
                continue
            items = {i.strip() for f in fs for i in (f["items"] or "").split(",") if i.strip()}
            buy = sum(f["insider_buy_usd"] or 0 for f in fs if f["form"] == "4")
            if items & NEGATIVE_8K or any(f["form"].upper().startswith(ed.OFFERING_FORMS) for f in fs):
                out.append(self._reject(sym, "악재 공시 (증자/상폐/액면병합)"))
                continue
            if buy >= self.p["min_insider_usd"]:
                why_in = f"내부자 장내매수 ${buy:,.0f}"
            elif items & POSITIVE_8K:
                why_in = "8-K 중요 계약(1.01)"
            else:
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, bad, b.last))
                continue
            why = shared_filter(sym, b.mid, ctx)
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date)
            run = (b.ask / pc - 1) * 100 if pc else 0.0
            if run > self.p["max_runup_pct"]:
                out.append(self._reject(sym, f"이미 {run:+.1f}% 반영", b.ask))
                continue
            price = b.ask
            out.append(Plan(self.name, sym, True, f"{why_in}, 반영 {run:+.1f}%",
                            score=min(buy / 10000, 50) + (5 if items & POSITIVE_8K else 0), price=price,
                            stop=price * (1 - self.p["stop_loss_pct"] / 100),
                            target=price * (1 + self.p["take_profit_pct"] / 100),
                            sell_from=ctx.regular_open - timedelta(minutes=self.p["sell_before_open_minutes"]),
                            deadline=ctx.deadline))
        return out


class DayDip(Strategy):
    name = "day_dip"

    def scan(self, ctx: Ctx) -> list[Plan]:
        latest = ctx.mv.latest_prices(ctx.trade_date, DAY)
        out = []
        for sym, last in latest.items():
            if sym in ctx.held or not last:
                continue
            # 유니버스 전체를 훑으므로 API 호출 없이 수집기 기록만 쓴다
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            if not pc:
                continue
            drop = (last / pc - 1) * 100
            if drop > -self.p["dip_pct"]:
                continue
            if ctx.mv.filings_for(sym, self.p["no_news_days"], ctx.now):
                out.append(self._reject(sym, f"{drop:.1f}% 급락이지만 최근 공시 있음", last))
                continue
            path = [p for _, p in ctx.mv.session_path(sym, ctx.trade_date, DAY)]
            n = self.p["stall_minutes"]
            if len(path) < n * 2 or min(path[-n:]) < min(path[:-n]):
                out.append(self._reject(sym, f"{drop:.1f}% 급락, 아직 저점 갱신 중", last))
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, bad, last))
                continue
            why = shared_filter(sym, b.mid, ctx)
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            price = min(b.ask, b.bid * 1.003)   # 매수호가 근처에 걸어 유동성 공급
            pre = ctx.bound(PRE)
            out.append(Plan(self.name, sym, True, f"공시 없이 {drop:.1f}% 급락 후 하락 멈춤",
                            score=-drop, price=price,
                            stop=price * (1 - self.p["stop_loss_pct"] / 100),
                            target=price + (pc - price) * self.p["retrace"],
                            sell_from=pre[0] if pre else ctx.regular_open,
                            deadline=min(ctx.deadline, ctx.regular_open)))
        return out


ALL = {cls.name: cls for cls in (OpeningPremium, FilingReaction, DayDip)}
