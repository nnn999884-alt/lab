"""가상 매매(S1 VWAP 눌림·S5 5분 눌림)를 모의매매 봇 전략으로 옮긴 것.

신호: 그날 처음으로 전날 대비 +5% 이상이면서 1분 거래대금 $5만 이상인 봉.
  S1: 신호 시점 VWAP −10% 지정가 (60분 유효)
  S5: 신호 5분 뒤 고점 대비 −10% 눌림 + 거래대금 유지(50%)면 그 가격 매수
청산은 공통 규칙(−50% 손절, +20%/+15% 목표, 08:20 KST 전량)을 따른다.
"""
from __future__ import annotations

from datetime import timedelta

from ..sessions import AFTER, DAY, PRE, REGULAR
from .strategies import Ctx, Plan, Strategy, next_day_open


class VirtualPullback(Strategy):
    name = "vpull"
    SESSIONS = {DAY, PRE, REGULAR, AFTER}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        rows = ctx.mv.q("SELECT symbol, ts, high, low, close, volume FROM bars_1m "
                        "WHERE trade_date=? AND ts<=? ORDER BY symbol, ts",
                        (ctx.trade_date.isoformat(), ctx.mv._asof()))
        by: dict[str, list[tuple]] = {}
        for sym, ts, hi, lo, c, v in rows:
            by.setdefault(sym, []).append((ts, hi, lo, c, v))
        out = []
        for sym, bars in by.items():
            if sym in ctx.held or len(bars) < 3:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            if not pc:
                continue
            try:
                b = ctx.mv.book(sym)
            except Exception:
                continue
            if not b.has_quote or b.spread_pct > 3:
                continue
            k = next((i for i, (ts, hi, lo, c, v) in enumerate(bars)
                      if (c / pc - 1) * 100 >= p["min_move_pct"] and c * v >= p["min_dollar"]), None)
            if k is None or k == 0:
                continue
            price = None
            why = ""
            deadline = min(ctx.now + timedelta(minutes=p["valid_minutes"]), next_day_open(ctx.now))
            nxt = k + 5
            if nxt < len(bars):
                hi = max(x[1] for x in bars[k:nxt + 1])
                c5 = bars[nxt][3]
                burst = sum(x[3] * x[4] for x in bars[max(0, k - 4):k + 1])
                recent = sum(x[3] * x[4] for x in bars[max(0, nxt - 4):nxt + 1])
                if c5 <= hi * (1 - p["dip_pct"] / 100) and recent >= burst * p["keep_ratio"]:
                    price = c5 * 1.005
                    why = f"신호 후 {p['dip_pct']:g}% 눌림·거래대금 유지 → 매수 (가상 5분 눌림)"
            if price is None:
                vol = sum(x[4] for x in bars[:k + 1]) or 1
                vwap = sum(x[3] * x[4] for x in bars[:k + 1]) / vol
                limit = vwap * (1 - p["vwap_dip_pct"] / 100)
                price = limit
                why = f"VWAP −{p['vwap_dip_pct']:g}% 지정가 (가상 VWAP 눌림)"
            out.append(Plan(self.name, sym, True, why, score=(price / pc - 1) * 100, price=price,
                            stop=price * (1 - p["stop_loss_pct"] / 100),
                            target=price * (1 + p["take_profit_pct"] / 100),
                            sell_from=deadline, deadline=deadline))
        return out
