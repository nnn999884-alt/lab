"""페니주 갭업 + 시가범위 돌파(ORB) 모멘텀 전략.

진입 조건 (모두 충족):
  1) 가격 min_price ~ max_price
  2) 전일 대비 +min_gap_pct% 이상
  3) 당일 거래량/거래대금 하한 충족
  4) 호가 스프레드 max_spread_pct% 이하
  5) 개장 후 opening_range_minutes 구간이 끝났고, 최근 봉 종가가 그 구간 고가를 돌파
  6) 최근 봉 거래량이 평균의 min_bar_rvol 배 이상
청산 조건 (하나라도): 손절 / 익절 / 트레일링 스탑 / 보유시간 초과 / 장마감 전 일괄청산
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import StrategyConfig
from .models import Bar, Position, Quote

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_market_hours(now: datetime) -> bool:
    n = now.astimezone(ET)
    return n.weekday() < 5 and MARKET_OPEN <= n.time() < MARKET_CLOSE


@dataclass
class Signal:
    ok: bool
    reason: str
    stop_price: float = 0.0


class PennyMomentumStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def check_entry(self, q: Quote, bars: list[Bar], now: datetime) -> Signal:
        c = self.cfg
        now_et = now.astimezone(ET)
        if now_et.time() >= _hhmm(c.no_new_entries_after):
            return Signal(False, "신규진입 마감시간")
        if not (c.min_price <= q.last <= c.max_price):
            return Signal(False, f"가격범위 밖 ${q.last:.4f}")
        if q.change_pct < c.min_gap_pct:
            return Signal(False, f"상승률 부족 {q.change_pct:.1f}%")
        if q.volume < c.min_day_volume or q.volume * q.last < c.min_dollar_volume:
            return Signal(False, "거래량 부족")
        if q.spread_pct > c.max_spread_pct:
            return Signal(False, f"스프레드 과다 {q.spread_pct:.2f}%")

        today = [b for b in bars if b.ts.astimezone(ET).date() == now_et.date()
                 and b.ts.astimezone(ET).time() >= MARKET_OPEN]
        if not today:
            return Signal(False, "당일 봉 없음")
        open_dt = datetime.combine(now_et.date(), MARKET_OPEN, tzinfo=ET)
        or_bars = [b for b in today
                   if (b.ts.astimezone(ET) - open_dt).total_seconds() < c.opening_range_minutes * 60]
        after = today[len(or_bars):]
        if not or_bars or not after:
            return Signal(False, "시가범위 형성 중")

        or_high = max(b.high for b in or_bars)
        last = after[-1]
        if last.close <= or_high:
            return Signal(False, f"돌파 전 (ORH {or_high:.4f})")
        # 이미 많이 달려버린 경우 추격 금지 (돌파가 +5% 이내)
        if max(last.close, q.last) > or_high * 1.05:
            return Signal(False, "돌파 후 과열 (추격금지)")

        hist = today[-(c.rvol_lookback_bars + 1):-1]
        avg_vol = sum(b.volume for b in hist) / len(hist) if hist else 0
        if avg_vol <= 0 or last.volume / avg_vol < c.min_bar_rvol:
            return Signal(False, "돌파 봉 거래량 부족")

        stop = max(q.last * (1 - c.stop_loss_pct / 100), min(b.low for b in after[-3:]))
        return Signal(True, f"ORB 돌파 ORH={or_high:.4f} rvol={last.volume / avg_vol:.1f}", stop)

    def check_exit(self, pos: Position, q: Quote, now: datetime) -> str | None:
        c = self.cfg
        price = q.bid if q.bid > 0 else q.last
        pos.high_water = max(pos.high_water or pos.avg_price, q.last)
        now_et = now.astimezone(ET)

        if now_et.time() >= _hhmm(c.flatten_at):
            return "장마감 전 청산"
        stop = pos.meta.get("stop_price") or pos.avg_price * (1 - c.stop_loss_pct / 100)
        if price <= stop:
            return f"손절 ({pos.pnl_pct(price):.1f}%)"
        if pos.pnl_pct(price) >= c.take_profit_pct:
            return f"익절 ({pos.pnl_pct(price):.1f}%)"
        if pos.high_water > pos.avg_price and \
                price <= pos.high_water * (1 - c.trailing_stop_pct / 100):
            return f"트레일링 스탑 (고점 {pos.high_water:.4f})"
        if pos.entry_time and (now - pos.entry_time).total_seconds() > c.max_hold_minutes * 60:
            return "보유시간 초과"
        return None
