"""2차 파동 B1: '첫 거래량은 패스' — 첫 거래량 폭발이 음봉으로 끝나고 다음날 거래가 말라붙은 종목을
첫날 종가의 60% 지정가로 기다렸다가 사서 최대 2거래일 안에 판다.

조건 (일봉, 사용자가 준 매매법을 그대로 규칙으로 + 2026-11-05 일봉 2년 격자 검증으로 수치 선택):
- 관심종목: D0 전 63거래일 종가 최고/최저 ≤ 3배(조용한 횡보), 그중 80% 이상의 날이 일 거래대금 $70만(≈10억원) 이하
- D0: 거래량 ≥ 63일 평균 × 5, 거래대금 ≥ $50만, 음봉 마감(종가 < 시가), 종가 $0.1~5
- D1: 거래량 ≤ D0 × 0.3 + 음봉 (거래 소멸 + 하락 지속)
- 진입: D2 ~ D11 (10거래일) 동안 가격이 D0 종가 × 0.6 이하로 내려오면 그 지정가에 매수 (종목·사건당 한 번)
- 청산: 익절 +15% / 손절 −30% / 체결 다음 거래일 정규장 끝까지 (보유 최대 2일)

검증 (비용 2%, 앞 1년 24.01~25.06 에서 고르고 뒤 1년 25.06~26.11 확인, 이웃 설정도 비슷한 자리):
  앞 n=28 평균 +4.9% 중앙 +5.4% 승률 64% / 뒤 n=51 평균 +0.3% 중앙 +0.7% 승률 55% — 뒤 기간엔 본전 근처.
  '거래량이 다시 붙을 때 매수' 쪽은 1만 칸 중 두 기간 다 플러스 0% 라 넣지 않음. 모의매매로 앞으로 쌓이는 결과를 볼 것.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..sessions import REGULAR
from .strategies import Ctx, Plan, Strategy, quote_check


class Wave2B1(Strategy):
    name = "wave2_b1"
    SESSIONS = {REGULAR}
    KEEP_OVERNIGHT = True      # 아침 전량 청산·진입 차단 제외
    SWING = True               # 손절·익절·보유 기한만 (장중 단타용 청산 규칙 제외)
    journal = None             # trader 가 붙여 준다 (같은 사건 재진입 막기)

    def __init__(self, params: dict):
        super().__init__(params)
        self._cache: tuple = (None, [])

    def setups(self, ctx: Ctx) -> list[dict]:
        """오늘 지정가를 걸어 둘 사건들 (하루 한 번 계산)."""
        if self._cache[0] == ctx.trade_date:
            return self._cache[1]
        p = self.p
        today = ctx.trade_date.isoformat()
        rows = ctx.mv.q("SELECT symbol, d, open, high, low, close, volume FROM daily_bars "
                        "WHERE d >= ? AND d < ? ORDER BY symbol, d",
                        ((ctx.trade_date - timedelta(days=130)).isoformat(), today))
        by: dict[str, list] = {}
        for s, d, o, h, l, c, v in rows:
            if o and c and v is not None:
                by.setdefault(s, []).append((d, o, h, l, c, v))
        out = []
        win = p["window_days"]
        for s, b in by.items():
            n = len(b)
            # D0 는 '오늘 기준 D2~D(win+1)' 이 되도록: 마지막 봉이 어제이므로 D0 인덱스 = n-2 ... n-1-win
            for i in range(max(64, n - 1 - win), n - 1):
                d0, d1 = b[i], b[i + 1]
                if not (0.1 <= d0[4] <= 5) or d0[4] >= d0[1]:
                    continue
                prev = b[i - 63:i]
                av = sum(x[5] for x in prev) / 63
                if av <= 0 or d0[5] < av * p["min_vol_x"] or d0[4] * d0[5] < p["min_dollar"]:
                    continue
                cl = [x[4] for x in prev]
                if max(cl) / min(cl) > p["max_box"]:
                    continue
                if sum(1 for x in prev if x[4] * x[5] <= p["quiet_dollar"]) < p["quiet_share"] * 63:
                    continue
                if not (d1[5] <= d0[5] * p["max_d1_vol"] and d1[4] < d1[1]):
                    continue
                lim = d0[4] * p["limit_ratio"]
                if any(x[3] <= lim for x in b[i + 2:]):
                    continue                           # 이미 지난 날 지정가에 닿았음 = 그 기회는 지나감
                out.append({"symbol": s, "d0": d0[0], "d0_close": d0[4], "limit": lim,
                            "vol_x": d0[5] / av, "d1_vol": d1[5] / d0[5]})
        self._cache = (ctx.trade_date, out)
        return out

    def _done(self, sym: str, d0: str) -> bool:
        j = self.journal
        if j is None:
            return False
        try:
            return bool(j.q("SELECT 1 FROM trades WHERE strategy=? AND symbol=? AND trade_date>? LIMIT 1",
                            (self.name, sym, d0)))
        except Exception:
            return False

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        out = []
        for e in self.setups(ctx):
            sym = e["symbol"]
            if sym in ctx.held or self._done(sym, e["d0"]):
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                continue
            if b.ask > e["limit"]:
                continue                               # 아직 지정가까지 안 내려옴 (조용히 기다림, 기록 안 남김)
            price = min(e["limit"], b.ask)
            deadline = self._deadline(ctx)
            out.append(Plan(self.name, sym, True,
                            f"2차 파동 B1: {e['d0']} 첫 거래량 {e['vol_x']:.0f}배 음봉 → 다음날 거래 {e['d1_vol']:.0%} 로 소멸 "
                            f"→ 첫날 종가 ${e['d0_close']:.3g} 의 {p['limit_ratio']:.0%} 지정가",
                            score=e["vol_x"], price=price,
                            stop=price * (1 - p["stop_loss_pct"] / 100),
                            target=price * (1 + p["take_profit_pct"] / 100),
                            sell_from=deadline, deadline=deadline,
                            context={**e, "ask": b.ask, "bid": b.bid, "spread_pct": b.spread_pct}))
        return out

    @staticmethod
    def _deadline(ctx: Ctx) -> datetime:
        """체결 다음 거래일 정규장 끝 (보유 최대 2거래일)."""
        days = sorted(d for d, x in ctx.cal.days.items() if d > ctx.trade_date and REGULAR in x.sessions)
        if days:
            return ctx.cal.days[days[0]].sessions[REGULAR][1]
        return ctx.now + timedelta(days=2)
