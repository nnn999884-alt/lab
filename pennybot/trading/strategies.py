"""페니 급등주 전략: 데이마켓에 사서 그날 밤(프리마켓~정규장 개장 직후)에 판다.

급등주는 평균적으로 장중에 되밀리고(소형주 +50% 갭 3,133건 연구), 급등 직후 회사가 주식을 파는 일이 많다.
그래서 (1) 개장 전 개인 매수세가 몰릴 때 팔고, (2) '지금 주식을 팔 수 없는' 회사만 고르는 것이 핵심이다.

  runner_overnight  : 전날 급등(+30%↑, 고가권 마감, 거래 회전 큼) + 희석 여력 낮음 → 데이마켓 매수 → 개장 무렵 매도
  dilution_catalyst : ATM 종료 · 증자 등록 철회 · 공모 철회 · 자사주 매입 공시 → 데이마켓 매수 → 프리마켓~개장 매도
  day_ignition      : 아시아 시간(데이마켓)에 새 호재 공시나 토스 거래대금 상위로 막 움직이기 시작 → 미국 군중보다 먼저
  day_dip           : 데이마켓 급락 반등 (기본 꺼짐 — 페니주 급락은 증자·펌프 붕괴인 경우가 많다)

모든 숫자는 백테스트 전 초안 (config.trading.strategies).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..screener import edgar as ed
from ..sessions import DAY, PRE, REGULAR, SessionCalendar

POSITIVE_DILUTION = {"ATM_TERMINATED", "REGISTRATION_WITHDRAWN", "OFFERING_TERMINATED", "BUYBACK"}
POSITIVE_NEWS = {"MATERIAL_CONTRACT", "COMPLIANCE_REGAINED", "FDA_APPROVAL", "MATERIAL_NEWS"}
FINANCING_WORDS = {"ATM_NEW", "REGISTERED_DIRECT", "PUBLIC_OFFERING", "SECURITIES_PURCHASE_AGREEMENT",
                   "PRIVATE_PLACEMENT", "EQUITY_LINE", "TAKEDOWN"}
SESSION_KO = {"DAY": "데이마켓", "PRE": "프리마켓", "REGULAR": "정규장", "AFTER": "애프터마켓", "CLOSED": "휴장"}
LABEL = {"ATM_TERMINATED": "ATM 종료", "REGISTRATION_WITHDRAWN": "증자 등록 철회",
         "OFFERING_TERMINATED": "공모 철회", "BUYBACK": "자사주 매입"}


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
    scale_out: float = 1.0      # 익절가에서 팔 비율 (1 = 전량)
    trail_pct: float = 0.0      # 분할 익절 후 나머지: 고점 대비 이만큼 빠지면 청산 (0 = 없음)


@dataclass
class Ctx:
    now: datetime
    trade_date: date
    session: str
    cal: SessionCalendar
    mv: object                       # MarketView
    tcfg: object                     # TradingConfig
    fund: object = None              # Fundamentals (없으면 희석 프로필 없이)
    held: set = field(default_factory=set)

    def bound(self, session: str):
        return self.cal.bounds(self.trade_date, session)

    @property
    def regular_open(self) -> datetime:
        return self.bound(REGULAR)[0]


def events_of(f: dict) -> set[str]:
    return set((f.get("events") or "").split(",")) - {""}


def quote_check(sym: str, ctx: Ctx):
    b = ctx.mv.book(sym)
    if not b.has_quote:
        return b, "데이마켓 호가 없음"
    cap = ctx.tcfg.spread_cap(ctx.session)
    if b.spread_pct > cap:
        return b, f"스프레드 {b.spread_pct:.1f}% (세션 기준 {cap:.1f}%)"
    return b, None


def dilution_gate(sym: str, price: float, ctx: Ctx, has_catalyst: bool, max_flags: int = 2) -> tuple[str | None, str]:
    """(탈락 사유 또는 None, 설명). 희석 프로필이 있으면 그것으로, 없으면 수집기 공시로 판단.
    희석 점수 자체로는 더는 거르지 않는다 (RENT 사례: 100점이어도 +32% 지속 — 점수가 높아도 주가가 오르는 건
    오른다고 판단, 참고용 note 로만 남김). 다만 상폐·사기성 공시처럼 '차단' 등급과, 펌프 위험 신호 개수는 그대로 막는다."""
    t = ctx.tcfg
    if not (t.min_price <= price <= t.max_price):
        return f"가격범위 밖 ${price:.4f}", ""
    prof = ctx.fund.profile(sym, price, ctx.trade_date) if ctx.fund else None
    if prof is not None:
        note = f"희석 {prof.score:.0f}점" + (f" [{', '.join(prof.recent_positive)}]" if prof.recent_positive else "")
    else:
        filings = [ed.Filing(f["form"], datetime.fromisoformat(f["accepted"]).date(), "", "", f["items"] or "")
                   for f in ctx.mv.filings_for(sym, 120, ctx.now)]
        rep = ed.evaluate(filings, ctx.trade_date)
        if rep.blocked:
            return "희석/상폐 공시: " + "; ".join(r for r in rep.reasons if r.startswith("차단")), ""
        note = "희석 프로필 없음(SEC_USER_AGENT 미설정)"
    if ctx.fund:
        flags = ctx.fund.flags(sym, ctx.trade_date, has_catalyst)
        if len(flags) > max_flags:
            return "펌프 위험 신호: " + ", ".join(flags), note
    return None, note


class Strategy:
    name = ""

    def __init__(self, params: dict):
        self.p = params

    def scan(self, ctx: Ctx) -> list[Plan]:
        raise NotImplementedError

    def _reject(self, sym, why, price=0.0) -> Plan:
        return Plan(self.name, sym, False, why, price=price)

    def _plan(self, ctx: Ctx, sym: str, price: float, reason: str, score: float, sell_from: datetime) -> Plan:
        p = self.p
        deadline = ctx.regular_open + timedelta(minutes=p.get("exit_after_open_minutes",
                                                               ctx.tcfg.forced_exit_minutes))
        return Plan(self.name, sym, True, reason, score=score, price=price,
                    stop=price * (1 - p["stop_loss_pct"] / 100),
                    target=price * (1 + p["take_profit_pct"] / 100),
                    sell_from=min(sell_from, deadline), deadline=deadline,
                    scale_out=p.get("scale_out_pct", 1.0), trail_pct=p.get("trail_pct", 0.0))

    def _entry_price(self, b) -> float:
        # 스프레드가 좁으면 ask 로, 넓으면 중간가 근처 지정가 → 거기서 entry_discount_pct 만큼 더 낮게 건다
        # (체결 확률은 낮아지지만 더 싸게 산다 — 안 붙으면 entry_ttl_seconds 뒤 취소하고 다음 스캔에서 다시 본다)
        ref = b.ask if b.spread_pct <= 1.0 else min(b.ask, b.mid * 1.002)
        return ref * (1 - self.p.get("entry_discount_pct", 0.0) / 100)

    def _sell_from(self, ctx: Ctx) -> datetime:
        return ctx.regular_open - timedelta(minutes=self.p["sell_before_open_minutes"])

    def _since_prev_close(self, ctx: Ctx) -> datetime:
        prev = ctx.mv.prev_trade_date(ctx.trade_date)
        reg = ctx.cal.bounds(prev, REGULAR) if prev else None
        return reg[1] if reg else ctx.now - timedelta(hours=18)


class RunnerOvernight(Strategy):
    name = "runner_overnight"

    def scan(self, ctx: Ctx) -> list[Plan]:
        prev = ctx.mv.prev_trade_date(ctx.trade_date)
        if not prev:
            return []
        out = []
        for r in ctx.mv.runners(prev, self.p["min_runner_pct"]):
            sym = r["symbol"]
            if sym in ctx.held or r["change_pct"] < self.p["min_runner_pct"]:
                continue
            if r.get("unverified"):
                out.append(self._reject(sym, "전날 마감 위치·거래량 확인 불가 (수집기 기록 없음)"))
                continue
            if r.get("close_loc") is not None and r["close_loc"] < self.p["min_close_loc"]:
                out.append(self._reject(sym, f"전날 +{r['change_pct']:.0f}%였지만 고가 대비 밀려 마감"))
                continue
            sh = ctx.fund.shares(sym, ctx.trade_date) if ctx.fund else None
            rot = r["volume"] / sh if sh and r.get("volume") else None
            if rot is not None and rot < self.p["min_rotation"]:
                out.append(self._reject(sym, f"거래 회전 {rot:.2f}회 (약함)"))
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, bad, b.last))
                continue
            chg = (b.mid / r["close"] - 1) * 100
            if not (self.p["min_day_change_pct"] <= chg <= self.p["max_day_change_pct"]):
                out.append(self._reject(sym, f"데이마켓 {chg:+.1f}% (전날 종가 대비, 범위 밖)", b.mid))
                continue
            recent = ctx.mv.filings_for(sym, 2, ctx.now)
            if any(events_of(f) & FINANCING_WORDS for f in recent):
                out.append(self._reject(sym, "급등 후 증자 공시 나옴", b.mid))
                continue
            why, note = dilution_gate(sym, b.mid, ctx, has_catalyst=bool(recent))
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            sr, sr5 = ctx.mv.short_ratio(sym, prev)
            if sr is not None and sr >= self.p["max_short_ratio"]:
                out.append(self._reject(sym, f"공매도 비율 {sr:.0%} (오버나이트엔 다음 날 시가가 나빴음)", b.mid))
                continue
            if sr is not None and sr5 and sr / sr5 >= self.p["max_short_surge"]:
                out.append(self._reject(sym, f"공매도가 평소 대비 {sr / sr5:.1f}배 급증", b.mid))
                continue
            out.append(self._plan(ctx, sym, self._entry_price(b),
                                  f"전날 +{r['change_pct']:.0f}%" + (f", 회전 {rot:.1f}회" if rot else "")
                                  + f", 데이마켓 {chg:+.1f}%, {note}",
                                  score=r["change_pct"] / 10 + (rot or 0), sell_from=self._sell_from(ctx)))
        return out


class DilutionCatalyst(Strategy):
    name = "dilution_catalyst"

    def scan(self, ctx: Ctx) -> list[Plan]:
        out, seen = [], set()
        for f in ctx.mv.filings_since(self._since_prev_close(ctx)):
            sym, events = f["symbol"], events_of(f)
            hits = events & POSITIVE_DILUTION
            if not sym or sym in ctx.held or sym in seen or not hits:
                continue
            seen.add(sym)
            if events & FINANCING_WORDS:
                out.append(self._reject(sym, "호재와 증자 문구가 같이 있음 (확인 필요)"))
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, bad, b.last))
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date)
            run = (b.ask / pc - 1) * 100 if pc else 0.0
            if run > self.p["max_runup_pct"]:
                out.append(self._reject(sym, f"이미 {run:+.1f}% 반영", b.ask))
                continue
            why, note = dilution_gate(sym, b.mid, ctx, has_catalyst=True)
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            out.append(self._plan(ctx, sym, self._entry_price(b),
                                  f"{', '.join(LABEL[h] for h in sorted(hits))} "
                                  f"({SESSION_KO.get(f['session'], f['session'])} 공개), 반영 {run:+.1f}%, {note}",
                                  score=20 + (10 if "ATM_TERMINATED" in hits else 0) - run,
                                  sell_from=self._sell_from(ctx)))
        return out


class DayIgnition(Strategy):
    name = "day_ignition"

    def scan(self, ctx: Ctx) -> list[Plan]:
        catalysts: dict[str, str] = {}
        for f in ctx.mv.filings_since(self._since_prev_close(ctx)):
            ev = events_of(f)
            if not f["symbol"] or ev & FINANCING_WORDS:
                continue
            if f["form"] == "4" and (f["insider_buy_usd"] or 0) >= self.p["min_insider_usd"]:
                catalysts[f["symbol"]] = "내부자 장내매수"
            elif ev & POSITIVE_NEWS and not (f.get("direction") or 0) < 0:
                catalysts[f["symbol"]] = "호재 보도자료" if f["form"] == "NEWS" else "호재 공시"
        for sym in ctx.mv.toss_hot(ctx.trade_date):
            catalysts.setdefault(sym, "토스 거래대금 상위")
        out = []
        for sym, cat in catalysts.items():
            if sym in ctx.held:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date)
            path = [p for _, p in ctx.mv.session_path(sym, ctx.trade_date, DAY)]
            if not pc or len(path) < 5:
                continue
            last, hi = path[-1], max(path)
            move = (last / pc - 1) * 100
            if move < self.p["min_move_pct"]:
                continue
            if move > self.p["max_move_pct"]:
                out.append(self._reject(sym, f"{cat}, 이미 {move:+.1f}% (늦음)", last))
                continue
            if last < hi * (1 - self.p["max_pullback_pct"] / 100):
                out.append(self._reject(sym, f"{cat}, 고점 대비 밀림", last))
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, bad, last))
                continue
            news = cat != "토스 거래대금 상위"     # 공시·보도자료 촉매 여부
            why, note = dilution_gate(sym, b.mid, ctx, has_catalyst=news)
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            out.append(self._plan(ctx, sym, self._entry_price(b), f"{cat}, 데이마켓 {move:+.1f}%, {note}",
                                  score=move + (10 if news else 0), sell_from=self._sell_from(ctx)))
        return out


class DayDip(Strategy):
    name = "day_dip"

    def scan(self, ctx: Ctx) -> list[Plan]:
        latest = ctx.mv.latest_prices(ctx.trade_date, DAY)
        out = []
        for sym, last in latest.items():
            if sym in ctx.held or not last:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)   # 유니버스 전체 → API 호출 없이
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
            why, note = dilution_gate(sym, b.mid, ctx, has_catalyst=False, max_flags=3)
            if why:
                out.append(self._reject(sym, why, b.mid))
                continue
            price = min(b.ask, b.bid * 1.003)
            pre = ctx.bound(PRE)
            plan = self._plan(ctx, sym, price, f"공시 없이 {drop:.1f}% 급락 후 하락 멈춤, {note}", -drop,
                              sell_from=pre[0] if pre else ctx.regular_open)
            plan.target = price + (pc - price) * self.p["retrace"]
            plan.deadline = min(plan.deadline, ctx.regular_open)
            plan.sell_from = min(plan.sell_from, plan.deadline)
            out.append(plan)
        return out


ALL = {cls.name: cls for cls in (RunnerOvernight, DilutionCatalyst, DayIgnition, DayDip)}
