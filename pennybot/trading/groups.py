"""상황별 전략 3개 — 눌림 · 역추세 반등 · 급등. 청산 목표·손절·트레일을 각자 다르게 가져간다.

같은 종목이 여러 재료에 걸리면 점수가 높은 하나만 남기고, 그룹의 목표·손절로 덮어쓴다.
청산의 공통 안전장치(−50% 하드 손절, 08:20 KST 전량, 비데이 진입만 시간 손절)는 그대로 적용된다.
"""
from __future__ import annotations

from ..sessions import AFTER, DAY, PRE, REGULAR
from .strategies import (Ctx, DayDip, DayIgnition, DilutionCatalyst, Plan, RegularMomentum, RunnerOvernight,
                         Scalp, SteadyTrend, Strategy)
from .setups import Accumulation, News, Reversal, SessionGap, Squeeze


class Group(Strategy):
    SESSIONS = {DAY, PRE, REGULAR, AFTER}
    SOURCES: tuple = ()

    def scan(self, ctx: Ctx) -> list[Plan]:
        best: dict[str, Plan] = {}
        for cls in self.SOURCES:
            if ctx.session not in cls.SESSIONS:
                continue
            params = ctx.tcfg.strategies.get(cls.name) or {}
            try:
                plans = cls(params).scan(ctx)
            except Exception:
                continue
            for p in plans:
                if not p.ok:
                    continue
                cur = best.get(p.symbol)
                if cur is None or p.score > cur.score:
                    p.context = {**p.context, "source": cls.name}
                    best[p.symbol] = p
        out = sorted(best.values(), key=lambda p: p.score, reverse=True)[:int(self.p.get("max_candidates", 3))]
        for p in out:
            p.strategy = self.name
            p.stop = p.price * (1 - self.p["stop_loss_pct"] / 100)
            p.target = p.price * (1 + self.p["take_profit_pct"] / 100)
            p.scale_out = self.p.get("scale_out_pct", 1.0)
            p.trail_pct = self.p.get("trail_pct", 0.0)
        return out


class Pullback(Group):
    name = "pullback"
    SOURCES = (Scalp, SteadyTrend, SessionGap, Accumulation, RegularMomentum)


class Rebound(Group):
    name = "rebound"
    SOURCES = (Reversal, DayDip)


class Surge(Group):
    name = "surge"
    SOURCES = (News, Squeeze, RunnerOvernight, DayIgnition, DilutionCatalyst)
