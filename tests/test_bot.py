import json
from datetime import datetime, timedelta

import pytest

from pennybot.brokers.paper import PaperBroker
from pennybot.config import Config, RiskConfig, StrategyConfig
from pennybot.engine import Engine
from pennybot.models import Bar, Position, Quote
from pennybot.risk import RiskManager
from pennybot.strategy import ET, PennyMomentumStrategy

DAY = datetime(2026, 10, 21, tzinfo=ET)  # 수요일


def at(h, m):
    return DAY.replace(hour=h, minute=m)


def make_bars(breakout=True):
    """09:30~09:44 시가범위(고가 2.00), 이후 횡보, 마지막 봉에서 거래량 동반 돌파."""
    bars = []
    t = at(9, 30)
    for i in range(15):
        bars.append(Bar(t + timedelta(minutes=i), 1.90, 2.00, 1.85, 1.95, 100_000))
    for i in range(15, 40):
        bars.append(Bar(t + timedelta(minutes=i), 1.95, 1.99, 1.92, 1.96, 50_000))
    last_close = 2.04 if breakout else 1.98
    bars.append(Bar(t + timedelta(minutes=40), 1.97, 2.05, 1.96, last_close, 300_000))
    return bars


def quote(last=2.04, prev=1.60, bid=None, ask=None, vol=5_000_000):
    return Quote("TEST", last, bid or last * 0.998, ask or last * 1.002, vol, prev)


@pytest.fixture
def strat():
    return PennyMomentumStrategy(StrategyConfig())


def test_entry_on_breakout(strat):
    sig = strat.check_entry(quote(), make_bars(), at(10, 11))
    assert sig.ok, sig.reason
    assert 0 < sig.stop_price < 2.04


@pytest.mark.parametrize("q,reason", [
    (quote(last=6.0, prev=5.0), "가격범위"),
    (quote(prev=2.0), "상승률"),
    (quote(vol=10_000), "거래량"),
    (quote(bid=1.95, ask=2.10), "스프레드"),
])
def test_entry_filters(strat, q, reason):
    sig = strat.check_entry(q, make_bars(), at(10, 11))
    assert not sig.ok and reason in sig.reason


def test_no_entry_without_breakout(strat):
    sig = strat.check_entry(quote(last=1.98), make_bars(breakout=False), at(10, 11))
    assert not sig.ok and "돌파 전" in sig.reason


def test_no_entry_late_day(strat):
    assert not strat.check_entry(quote(), make_bars(), at(15, 5)).ok


def test_exit_rules(strat):
    now = at(11, 0)
    p = Position("TEST", 100, 2.00, entry_time=at(10, 30))
    assert strat.check_exit(p, quote(last=1.88), now).startswith("손절")
    p = Position("TEST", 100, 2.00, entry_time=at(10, 30))
    assert strat.check_exit(p, quote(last=2.30), now).startswith("익절")
    p = Position("TEST", 100, 2.00, entry_time=at(10, 30), high_water=2.20)
    assert strat.check_exit(p, quote(last=2.10), now).startswith("트레일링")
    p = Position("TEST", 100, 2.00, entry_time=at(10, 30))
    assert strat.check_exit(p, quote(last=2.02), at(15, 51)) == "장마감 전 청산"
    assert strat.check_exit(p, quote(last=2.02), now) is None


def test_risk_limits(tmp_path):
    rm = RiskManager(RiskConfig(max_position_usd=200, max_daily_loss_usd=100), str(tmp_path))
    assert rm.size(2.0, 1.9, 10_000) == 100            # 금액 상한 200$
    assert rm.size(2.0, 1.0, 10_000) == 50             # 손절폭 1$ → 손실 50$ 이내
    rm.record_exit(-100)
    ok, why = rm.can_open(0)
    assert not ok and "손실한도" in why
    rm2 = RiskManager(RiskConfig(), str(tmp_path))
    (tmp_path / "KILL").touch()
    assert not rm2.can_open(0)[0]


class FakeFeed:
    def __init__(self):
        self.q = quote()
        self.bars = make_bars()

    def get_quote(self, symbol):
        return self.q

    def get_bars(self, symbol, interval_minutes=1, count=100):
        return self.bars


def test_engine_round_trip(tmp_path):
    feed = FakeFeed()
    broker = PaperBroker(feed, cash=1000)
    cfg = Config(watchlist=["TEST"], state_dir=str(tmp_path))
    clock = [at(10, 11)]
    eng = Engine(cfg, broker, clock=lambda: clock[0])

    eng.tick()
    assert "TEST" in broker.positions
    qty = broker.positions["TEST"].qty
    assert qty * 2.05 <= cfg.risk.max_position_usd

    feed.q = quote(last=2.40)  # +17% → 익절
    clock[0] = at(10, 30)
    eng.tick()
    assert "TEST" not in broker.positions
    assert eng.risk.realized_pnl > 0
    assert broker.cash > 1000


def test_engine_uses_screener_watchlist(tmp_path):
    (tmp_path / "watchlist.json").write_text(json.dumps({"date": "2026-10-21", "picks": [
        {"symbol": "TEST", "pivot": 2.00, "avg_volume": 4_000_000, "score": 80}]}))
    feed = FakeFeed()
    feed.q = quote(last=2.04, prev=2.00)   # 갭 없음: ORB 라면 진입 불가, 피벗 돌파로는 진입
    broker = PaperBroker(feed, cash=1000)
    cfg = Config(watchlist=["AUTO"], state_dir=str(tmp_path))
    eng = Engine(cfg, broker, clock=lambda: at(10, 11))
    eng.tick()
    assert "TEST" in broker.positions
    assert eng.meta["TEST"]["stop_price"] >= 2.00 * 0.97
