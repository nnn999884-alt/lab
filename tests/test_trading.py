from datetime import date, datetime

import pytest

from pennybot.config import TradingConfig
from pennybot.sessions import ET, SessionCalendar
from pennybot.trading import Control, Journal, PaperExchange, Trader
from pennybot.trading.exchange import Book
from pennybot.trading.risk import Account, Risk

D = date(2026, 10, 21)
PREV = date(2026, 10, 20)


def et(h, m, d=D):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


class FakeMarket:
    def __init__(self):
        self.books = {}

    def set(self, sym, bid, ask, size=10_000, last=None):
        self.books[sym] = Book(sym, bid, ask, size, size, last or (bid + ask) / 2)

    def book(self, sym):
        return self.books.get(sym, Book(sym, 0, 0, 0, 0, 0))


class FakeMV:
    def __init__(self, market):
        self.m = market
        self.attn = [("PEN", 3)]
        self.closes = {"PEN": 1.00, "IWM": 200.0, "DIP": 2.00}
        self.filings = []
        self.recent = {}
        self.latest = {}
        self.path = []

    def prev_trade_date(self, d): return PREV
    def attention(self, prev, limit=60): return self.attn
    def prev_close(self, sym, d, allow_api=True): return self.closes.get(sym)
    def ret_5d(self, sym, d): return 0.1
    def filings_for(self, sym, days, now): return self.recent.get(sym, [])
    def filings_since(self, since): return self.filings
    def latest_prices(self, d, session): return self.latest
    def session_path(self, sym, d, session): return [(str(i), p) for i, p in enumerate(self.path)]
    def book(self, sym): return self.m.book(sym)


@pytest.fixture
def env(tmp_path):
    market = FakeMarket()
    market.set("IWM", 199.9, 200.1)
    clock = [et(1, 0)]                       # 데이마켓 (한국 14:00)
    tcfg = TradingConfig(paper_cash=10_000)
    tcfg.strategies["filing_reaction"]["enabled"] = False
    tcfg.strategies["day_dip"]["enabled"] = False
    ex = PaperExchange(market, 10_000, tmp_path / "paper.json")
    j = Journal(tmp_path / "t.db")
    ctl = Control(tmp_path)
    mv = FakeMV(market)
    tr = Trader(ex, j, SessionCalendar(), mv, tcfg, ctl, clock=lambda: clock[0])
    return tr, market, mv, clock, j, ctl, ex


def test_full_cycle_day_buy_premarket_sell(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "OPEN" and t["entry_session"] == "DAY" and t["qty"] > 0
    assert t["qty"] * 1.035 <= 300                     # 종목당 상한 $300
    # 프리마켓 초반: 아직 매도 구간 전, 손절·익절도 아님 → 유지
    clock[0] = et(6, 0)
    market.set("PEN", 1.05, 1.07)
    tr.tick()
    assert j.q("SELECT status FROM trades")[0]["status"] == "OPEN"
    # 개장 30분 전(09:00 ET)부터 매도 구간 → bid 에 매도
    clock[0] = et(9, 1)
    market.set("PEN", 1.07, 1.08)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "매도 구간" and t["exit_session"] == "PRE"
    assert t["pnl"] > 0
    assert j.q("SELECT COUNT(*) AS n FROM signals")[0]["n"] >= 1


def test_unfilled_entry_cancelled_at_session_change(env):
    tr, market, mv, clock, j, ctl, ex = env
    tr.tcfg.entry_ttl_seconds = 10_000
    market.set("PEN", 1.00, 1.025)           # 스프레드 2.5% → 중간가 근처 지정가, ask 가 높아 미체결
    clock[0] = et(3, 20)
    tr.tick()
    assert len(tr.working) == 1
    clock[0] = et(4, 1)                      # 데이 → 프리
    tr.tick()
    assert not tr.working
    assert j.q("SELECT status FROM trades")[0]["status"] == "CANCELED"
    assert ex.open_orders() == []


def test_stop_loss_and_forced_market_exit(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    # 손절가 아래로 → 공격적 지정가 (bid × 0.99) 로 즉시 매도
    clock[0] = et(5, 0)
    market.set("PEN", 0.90, 0.92)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "손절" and t["pnl"] < 0


def test_forced_exit_market_order_when_no_quote_in_regular(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    tr.tcfg.forced_exit_minutes = 0
    clock[0] = et(9, 31)                     # 정규장, 강제 청산 시각 지남
    market.set("PEN", 0, 0)                  # 호가 없음
    tr.tick()
    o = j.q("SELECT * FROM orders WHERE purpose='exit'")
    assert o and o[-1]["type"] == "MARKET"


def test_kill_switch_blocks_entries_and_flatten_sells(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    ctl.update(kill=True)
    tr.tick()
    assert j.q("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 0
    ctl.update(kill=False)
    tr._next.clear()
    tr.tick()
    assert j.q("SELECT status FROM trades")[0]["status"] == "OPEN"
    ctl.update(flatten=True)
    clock[0] = et(2, 0)
    tr.tick()
    assert j.q("SELECT exit_reason, status FROM trades")[0] == {"exit_reason": "수동 전량 청산", "status": "CLOSED"}
    tr.tick()
    assert ctl.load()["flatten"] is False


def test_regime_gate(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    market.set("IWM", 196.0, 196.2)           # −2%
    tr.tick()
    assert j.q("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 0
    assert "IWM" in tr.status["regime"]


def test_filters_reject_dilution_and_chase(env):
    tr, market, mv, clock, j, ctl, ex = env
    mv.attn = [("PEN", 1), ("HOT", 2)]
    mv.closes["HOT"] = 1.0
    market.set("PEN", 1.03, 1.035)
    market.set("HOT", 1.30, 1.32)             # +31% → 추격 금지
    mv.recent["PEN"] = [{"form": "424B5", "items": "", "accepted": et(0, 0).isoformat()}]
    tr.tick()
    sig = {r["symbol"]: r["reason"] for r in j.q("SELECT symbol, reason FROM signals")}
    assert "희석" in sig["PEN"] and "범위 밖" in sig["HOT"]
    assert j.q("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 0


def test_filing_reaction_and_day_dip(env):
    tr, market, mv, clock, j, ctl, ex = env
    tr.tcfg.strategies["opening_premium"]["enabled"] = False
    tr.tcfg.strategies["filing_reaction"]["enabled"] = True
    tr.tcfg.strategies["day_dip"]["enabled"] = True
    mv.closes["INS"] = 2.0
    market.set("INS", 2.05, 2.08)
    mv.filings = [{"symbol": "INS", "form": "4", "items": "", "accepted": "", "session": "AFTER",
                   "trade_date": str(PREV), "insider_buy_usd": 50_000}]
    market.set("DIP", 1.70, 1.71)
    mv.latest = {"DIP": 1.70}
    mv.path = [1.9, 1.8, 1.6, 1.55, 1.6, 1.62, 1.65, 1.66, 1.68, 1.70] * 2   # 저점 후 반등 중
    tr.tcfg.strategies["day_dip"]["stall_minutes"] = 5
    tr.tick()
    rows = {r["symbol"]: r for r in j.q("SELECT * FROM trades")}
    assert rows["INS"]["strategy"] == "filing_reaction"
    assert rows["DIP"]["strategy"] == "day_dip"
    assert rows["DIP"]["target"] > 1.70 and datetime.fromisoformat(rows["DIP"]["deadline"]) <= et(9, 30)


def test_restart_recovers_working_orders(env, tmp_path):
    tr, market, mv, clock, j, ctl, ex = env
    tr.tcfg.entry_ttl_seconds = 10_000
    market.set("PEN", 1.00, 1.025)
    tr.tick()
    assert len(tr.working) == 1
    ex2 = PaperExchange(market, 10_000, tmp_path / "paper.json")
    tr2 = Trader(ex2, j, SessionCalendar(), mv, tr.tcfg, ctl, clock=lambda: clock[0])
    assert list(tr2.working) == list(tr.working)
    market.set("PEN", 1.00, 1.01)            # 이제 체결 가능
    tr2.tick()
    assert j.q("SELECT status FROM trades")[0]["status"] == "OPEN"


def test_risk_sizing_daily_loss_and_auto_disable(tmp_path):
    tcfg = TradingConfig()
    j = Journal(tmp_path / "r.db")
    risk = Risk(tcfg, j, Control(tmp_path))
    # 계좌 1만$: 전략 예산 4000/4=1000, 계좌 5%=500, 종목 상한 300 → $300, 손절 위험 1%=100$
    assert risk.size("opening_premium", 1.0, 0.94, Account(10_000, 10_000)) == 300
    assert risk.size("opening_premium", 1.0, 0.50, Account(10_000, 10_000)) == 200   # 100$ / 0.5
    for i in range(20):
        tid = j.open_trade(strategy="day_dip", symbol=f"S{i}", trade_date=str(D), planned_qty=10,
                           stop=0.9, target=1.2, sell_from=et(4, 0), deadline=et(9, 30), reason="", now=et(1, 0))
        j.record_entry_fill(tid, 10, 1.0, "DAY", et(1, 0))
        j.record_exit_fill(tid, 10, 0.98, "PRE", et(5, 0))
    ok, why = risk.strategy_enabled("day_dip")
    assert not ok and "자동 정지" in why
    tcfg.max_daily_loss_usd = 3
    assert risk.can_enter("opening_premium", "X", str(D), []) == "영업일 손실 한도 도달"


def test_paper_partial_fill_and_reject():
    m = FakeMarket()
    m.set("A", 1.0, 1.01, size=100)
    import tempfile
    from pathlib import Path
    ex = PaperExchange(m, 1000, Path(tempfile.mkdtemp()) / "p.json")
    o = ex.place("A", "BUY", 250, 1.01)
    assert o["filled"] == 100 and o["status"] == "OPEN"
    ex.sync()
    ex.sync()
    assert ex.order(o["order_id"])["status"] == "FILLED"
    assert ex.place("A", "SELL", 1000, 1.0)["status"] == "REJECTED"   # 보유보다 많이 매도
    assert ex.positions()["A"][0] == 250
