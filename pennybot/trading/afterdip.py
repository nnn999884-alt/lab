"""애프터마켓에서 밀려서 저가 근처로 마감한 종목을 사서 다음 거래일 프리마켓 시작에 판다.

2026-11-01 규칙 탐색(핸드오프 규칙 162개 x 4세션, 수수료 1.5% 차감, 시간순 3구간 모두 플러스만 통과)에서
애프터마켓 끝 매수 → 다음날 프리마켓 시작 매도만 9개 규칙이 통과했고 전부 같은 테마였다.
이 규칙: 애프터 세션 수익률 ≤ -2.7% 이고 종가가 그 세션 범위 하위 15% 이내 (3구간 평균 +1.8/+0.5/+0.4%,
승률 61/54/50%, 건수 381/441/328). 구간이 갈수록 약해지고(+1.8→+0.4%), 아래 스프레드 문제가 크다.

스프레드: 우리 호가 기록상 애프터 스프레드 중앙값이 10%, 1% 이하는 7%뿐이라 백테스트의 1.5% 비용 가정이
낙관적이다. 그래서 max_spread_pct 로 스프레드가 좁은 종목만 산다 — 모의매매로 실제로 남는지 확인이 필요하다.

백테스트는 애프터 마지막 봉 종가 매수를 가정했으므로 애프터 끝물(late_window_minutes)에만 신호를 본다.
KEEP_OVERNIGHT: 매일 아침 전량 청산(flatten_kst)·진입 차단(entry_cutoff_kst)에서 이 전략만 제외한다.
"""
from __future__ import annotations

from datetime import timedelta

from ..sessions import AFTER, PRE
from .strategies import Ctx, Plan, Strategy, quote_check


class AfterDip(Strategy):
    name = "after_dip"
    SESSIONS = {AFTER}
    KEEP_OVERNIGHT = True

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        after = ctx.bound(AFTER)
        if not after:
            return []
        if ctx.now < after[1] - timedelta(minutes=p["late_window_minutes"]):
            return []
        sell_from = self._next_pre_open(ctx)
        if sell_from is None:
            return []
        rows = ctx.mv.q(
            "SELECT symbol, ts, high, low, close, volume FROM bars_1m "
            "WHERE trade_date=? AND session=? AND ts<=? ORDER BY symbol, ts",
            (ctx.trade_date.isoformat(), AFTER, ctx.mv._asof()))
        by: dict[str, list[tuple]] = {}
        for sym, ts, hi, lo, c, v in rows:
            by.setdefault(sym, []).append((hi, lo, c))
        out = []
        for sym, bars in by.items():
            if sym in ctx.held or len(bars) < p["min_bars"] or not bars[0][2]:
                continue
            first, last = bars[0][2], bars[-1][2]
            hi, lo = max(b[0] for b in bars), min(b[1] for b in bars)
            if hi <= lo:
                continue
            ret = (last / first - 1) * 100
            loc = (last - lo) / (hi - lo)
            if ret > p["max_session_ret_pct"] or loc > p["max_close_loc"]:
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"애프터 {ret:+.1f}% 눌림 마감, {bad}", 0))
                continue
            if b.spread_pct > p["max_spread_pct"]:
                out.append(self._reject(sym, f"애프터 {ret:+.1f}% 눌림 마감, 스프레드 {b.spread_pct:.1f}% (상한 {p['max_spread_pct']:g}%)", b.mid))
                continue
            price = self._entry_price(b)
            deadline = sell_from + timedelta(minutes=p["exit_after_pre_minutes"])
            out.append(Plan(self.name, sym, True,
                            f"애프터 {ret:+.1f}% 하락·세션 하위 {loc * 100:.0f}% 마감 → 다음 프리마켓 시작에 매도",
                            score=-ret, price=price,
                            stop=price * (1 - p["stop_loss_pct"] / 100),
                            target=price * (1 + p["take_profit_pct"] / 100),
                            sell_from=sell_from, deadline=deadline,
                            context={"after_ret_pct": ret, "close_loc": loc, "spread_pct": b.spread_pct}))
        return out

    @staticmethod
    def _next_pre_open(ctx: Ctx):
        starts = [d.sessions[PRE][0] for day, d in ctx.cal.days.items() if day > ctx.trade_date and PRE in d.sessions]
        return min(starts) if starts else None
