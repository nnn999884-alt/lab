"""하나로 합친 전략 — 눌림(수급) 신호와 촉매(공시·스퀴즈·가격차·매집·역추세) 신호를 한 점수로 모아
상위 몇 개만 산다. 전략별 예산을 나누지 않고 계좌 단위로 굴리는 것이 목적이다.

각 신호는 기존 전략 클래스를 그대로 재사용하고, 같은 종목이 여러 신호에 걸리면 점수가 높은 하나만 남긴다.
청산은 공통 규칙(−50% 손절, +10% 뒤 −2% 트레일, 비데이 진입만 반등·매집 없음, 08:20 KST 전량)이 맡는다.
"""
from __future__ import annotations

from ..sessions import AFTER, DAY, PRE, REGULAR
from .strategies import (Ctx, Plan, Scalp, SteadyTrend, Strategy)
from .setups import Accumulation, News, Reversal, SessionGap, Squeeze

SOURCES = (Scalp, SteadyTrend, Squeeze, News, SessionGap, Accumulation, Reversal)


class Combined(Strategy):
    name = "combined"
    SESSIONS = {DAY, PRE, REGULAR, AFTER}

    def scan(self, ctx: Ctx) -> list[Plan]:
        best: dict[str, Plan] = {}
        for cls in SOURCES:
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
                p.reason = f"[{cls.name}] {p.reason}"
                p.context = {**p.context, "source": cls.name}
                cur = best.get(p.symbol)
                if cur is None or p.score > cur.score:
                    best[p.symbol] = p
        out = sorted(best.values(), key=lambda p: p.score, reverse=True)[:int(self.p.get("max_candidates", 5))]
        for p in out:
            p.strategy = self.name
        return out
