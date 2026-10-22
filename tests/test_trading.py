from datetime import date, datetime, timedelta

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
        # 전날 +45% 급등, 고가권 마감, 거래량 400만 주
        self.run = [{"symbol": "PEN", "prev_close": 0.70, "close": 1.02, "change_pct": 45.7,
                     "volume": 4_000_000, "close_loc": 0.85}]
        self.closes = {"PEN": 1.02, "IWM": 200.0, "DIP": 2.00}
        self.filings = []
        self.recent = {}
        self.latest = {}
        self.path = []
        self.hot = []

    def prev_trade_date(self, d): return PREV
    def runners(self, d, min_pct=30.0): return self.run
    def toss_hot(self, d): return self.hot
    def prev_close(self, sym, d, allow_api=True): return self.closes.get(sym)
    def ret_5d(self, sym, d): return 0.1
    def filings_for(self, sym, days, now): return self.recent.get(sym, [])
    def filings_since(self, since): return self.filings
    def latest_prices(self, d, session): return self.latest
    def session_path(self, sym, d, session): return [(str(i), p) for i, p in enumerate(self.path)]
    def book(self, sym): return self.m.book(sym)


class FakeProfile:
    def __init__(self, score, reasons=(), positive=()):
        self.score, self.reasons, self.recent_positive = score, list(reasons), list(positive)


class FakeFund:
    def __init__(self):
        self.scores = {}
        self.flag_map = {}
        self.share_map = {"PEN": 5_000_000}

    def profile(self, sym, price, d): return FakeProfile(self.scores.get(sym, 20))
    def flags(self, sym, d, has_catalyst): return self.flag_map.get(sym, [])
    def shares(self, sym, d): return self.share_map.get(sym)


@pytest.fixture
def env(tmp_path):
    market = FakeMarket()
    market.set("IWM", 199.9, 200.1)
    clock = [et(1, 0)]                       # 데이마켓 (한국 14:00)
    tcfg = TradingConfig(paper_cash=10_000)
    tcfg.strategies["dilution_catalyst"]["enabled"] = False
    tcfg.strategies["day_ignition"]["enabled"] = False
    ex = PaperExchange(market, 10_000, tmp_path / "paper.json")
    j = Journal(tmp_path / "t.db")
    ctl = Control(tmp_path)
    mv = FakeMV(market)
    fund = FakeFund()
    tr = Trader(ex, j, SessionCalendar(), mv, tcfg, ctl, clock=lambda: clock[0], fund=fund)
    tr.fake_fund = fund
    return tr, market, mv, clock, j, ctl, ex


def test_full_cycle_day_buy_premarket_sell(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "OPEN" and t["entry_session"] == "DAY" and t["qty"] > 0
    # 수량: 최악의 갭 35% 기준 손실 ≤ 계좌 1%($100) → 100 / (1.035×0.35) ≈ 276주
    assert t["qty"] == int(100 // (1.035 * 0.35))
    # 프리마켓 초반: 아직 매도 구간 전, 손절·익절도 아님 → 유지
    clock[0] = et(6, 0)
    market.set("PEN", 1.05, 1.06)
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
    # 손절가(−10%) 아래로 → 공격적 지정가 (bid × 0.99) 로 즉시 매도
    clock[0] = et(5, 0)
    market.set("PEN", 0.88, 0.90)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "손절" and t["pnl"] < 0


def test_forced_exit_market_order_when_no_quote_in_regular(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    clock[0] = et(9, 45)                     # 정규장, 매도 구간이지만 강제 청산 전 + 호가 없음 → 기다림
    market.set("PEN", 0, 0)
    tr.tick()
    assert not j.q("SELECT * FROM orders WHERE purpose='exit'")
    clock[0] = et(10, 1)                     # 러너는 개장 30분 뒤 강제 청산 → 호가 없으면 시장가
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


def test_runner_filters(env):
    tr, market, mv, clock, j, ctl, ex = env
    mv.run = [
        {"symbol": "PEN", "prev_close": 0.7, "close": 1.02, "change_pct": 45, "volume": 4e6, "close_loc": 0.9},
        {"symbol": "FADE", "prev_close": 1.0, "close": 1.4, "change_pct": 40, "volume": 4e6, "close_loc": 0.2},
        {"symbol": "THIN", "prev_close": 1.0, "close": 1.5, "change_pct": 50, "volume": 1e5, "close_loc": 0.9},
        {"symbol": "DILU", "prev_close": 1.0, "close": 1.5, "change_pct": 50, "volume": 9e6, "close_loc": 0.9},
        {"symbol": "PUMP", "prev_close": 1.0, "close": 2.0, "change_pct": 100, "volume": 9e6, "close_loc": 0.9},
        {"symbol": "OFFR", "prev_close": 1.0, "close": 1.5, "change_pct": 50, "volume": 9e6, "close_loc": 0.9},
    ]
    f = tr.fake_fund
    f.share_map.update({"THIN": 5e6, "DILU": 5e6, "PUMP": 5e6, "OFFR": 5e6, "FADE": 5e6})
    f.scores["DILU"] = 75
    f.flag_map["PUMP"] = ["상장 1년 이내", "해외 발행사(K3)", "발행주식 1,500만 주 미만"]
    mv.recent["OFFR"] = [{"form": "8-K", "items": "1.01", "accepted": et(0, 0).isoformat(),
                          "events": "REGISTERED_DIRECT,SECURITIES_PURCHASE_AGREEMENT"}]
    market.set("PEN", 1.03, 1.035)
    for s in ("FADE", "THIN", "DILU", "OFFR"):
        market.set(s, 1.50, 1.51)
    market.set("PUMP", 2.0, 2.01)
    tr.tick()
    sig = {r["symbol"]: r["reason"] for r in j.q("SELECT symbol, reason FROM signals")}
    assert "밀려 마감" in sig["FADE"]
    assert "회전" in sig["THIN"]
    assert "희석 위험 75점" in sig["DILU"]
    assert "펌프 위험" in sig["PUMP"]
    assert "증자 공시" in sig["OFFR"]
    assert [t["symbol"] for t in j.q("SELECT symbol FROM trades")] == ["PEN"]


def test_dilution_catalyst_and_ignition(env):
    tr, market, mv, clock, j, ctl, ex = env
    tr.tcfg.strategies["runner_overnight"]["enabled"] = False
    tr.tcfg.strategies["dilution_catalyst"]["enabled"] = True
    tr.tcfg.strategies["day_ignition"]["enabled"] = True
    mv.closes.update({"ATMX": 1.50, "IGN": 2.0})
    market.set("ATMX", 1.58, 1.59)                     # +6%: 아직 덜 반영
    market.set("IGN", 2.20, 2.21)
    mv.filings = [
        {"symbol": "ATMX", "form": "8-K", "items": "1.02", "accepted": "", "session": "DAY",
         "trade_date": str(D), "insider_buy_usd": None, "events": "ATM_TERMINATED", "direction": 1},
        {"symbol": "IGN", "form": "4", "items": "", "accepted": "", "session": "DAY",
         "trade_date": str(D), "insider_buy_usd": 60_000, "events": "", "direction": 1},
    ]
    mv.path = [2.0, 2.02, 2.05, 2.1, 2.15, 2.18, 2.2]   # 데이마켓에서 +10% 움직이는 중
    tr.tick()
    rows = {r["symbol"]: r for r in j.q("SELECT * FROM trades")}
    assert rows["ATMX"]["strategy"] == "dilution_catalyst" and "ATM 종료" in rows["ATMX"]["reason"]
    assert rows["IGN"]["strategy"] == "day_ignition" and "내부자" in rows["IGN"]["reason"]


def test_day_dip_off_by_default(env):
    tr, *_ = env
    assert tr.tcfg.strategies["day_dip"]["enabled"] is False
    assert not tr.risk.strategy_enabled("day_dip")[0]


def test_exit_reprice_counts_once_per_order_and_final_limit(env):
    """취소 처리 중(실거래 PENDING_CANCEL)에 매 틱 재취소·재호가 카운트가 쌓이면 안 된다."""
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    tid = j.q("SELECT id FROM trades")[0]["id"]
    clock[0] = et(9, 5)                      # 매도 구간
    market.set("PEN", 1.06, 1.07)
    ex.sync = lambda: None                   # 체결되지 않게
    ex.cancel = lambda oid: None             # 취소 요청이 '처리 중'으로 남는 상황
    tr.tick()
    for k in range(10):
        clock[0] = et(9, 7) + timedelta(seconds=5 * k)
        tr.tick()
    assert tr.exit_reprices.get(tid, 0) == 1


def test_bad_filing_while_holding_triggers_exit(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    clock[0] = et(5, 0)                       # 프리마켓, 아직 매도 구간 전
    mv.recent["PEN"] = [{"form": "424B5", "items": "", "accepted": et(4, 50).isoformat(),
                         "events": "TAKEDOWN"}]
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"].startswith("보유 중 악재 공시")


def test_unrealized_loss_counts_toward_daily_limit(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    tr.tcfg.max_daily_loss_usd = 20
    mv.run.append({"symbol": "NEW", "prev_close": 1, "close": 1.5, "change_pct": 50, "volume": 9e6,
                   "close_loc": 0.9})
    tr.fake_fund.share_map["NEW"] = 5e6
    market.set("NEW", 1.5, 1.505)
    market.set("PEN", 0.95, 0.96)              # 평가 손실 약 $22 (손절 −10% 전)
    clock[0] = et(1, 10)
    tr.tick()
    assert [t["symbol"] for t in j.q("SELECT symbol FROM trades")] == ["PEN"]
    assert any("평가 손실" in r["reason"] for r in j.q("SELECT reason FROM signals WHERE symbol='NEW'"))


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
    # 계좌 1만$: 금액 상한 $300, 갭 35% 기준 손실 ≤ $100 → 100 / 0.35 = 285주
    assert risk.size("runner_overnight", 1.0, 0.90, Account(10_000, 10_000)) == 285
    assert risk.size("runner_overnight", 1.0, 0.50, Account(10_000, 10_000)) == 200   # 손절폭 0.5 > 갭 0.35
    for i in range(20):
        tid = j.open_trade(strategy="day_ignition", symbol=f"S{i}", trade_date=str(D), planned_qty=10,
                           stop=0.9, target=1.2, sell_from=et(4, 0), deadline=et(9, 30), reason="", now=et(1, 0))
        j.record_entry_fill(tid, 10, 1.0, "DAY", et(1, 0))
        j.record_exit_fill(tid, 10, 0.98, "PRE", et(5, 0))
    ok, why = risk.strategy_enabled("day_ignition")
    assert not ok and "자동 정지" in why
    tcfg.max_daily_loss_usd = 3
    assert risk.can_enter("runner_overnight", "X", str(D), []).startswith("영업일 손실 한도 도달")


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


def test_modify_lineage_sums_fills_across_ids(env):
    """원주문 일부 체결 → 가격 정정(새 id) → 나머지 체결. 체결 합계와 평균가가 맞아야 한다."""
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    qty = j.q("SELECT qty FROM trades")[0]["qty"]
    clock[0] = et(9, 5)                              # 매도 구간
    market.set("PEN", 1.10, 1.12, size=100)          # 첫 주문은 100주만 체결
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["remaining"] == qty - 100
    w = next(iter(tr.working.values()))
    market.set("PEN", 1.05, 1.06, size=10_000)       # 가격이 내려가 기존 매도가(1.10)에는 안 팔림
    clock[0] = et(9, 6, ) + timedelta(seconds=30)    # 재호가 주기(60초) 경과 → 정정
    tr.tick()
    assert len(w.ids) == 2 and ex.order(w.ids[0])["status"] == "REPLACED"
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exited_qty"] == qty
    expected = (100 * 1.10 + (qty - 100) * 1.05) / qty
    assert abs(t["exit_price"] - expected) < 1e-9
    statuses = {r["status"] for r in j.q("SELECT status FROM orders WHERE purpose='exit'")}
    assert statuses == {"REPLACED", "FILLED"}


def test_cancel_rejected_is_retried(env):
    tr, market, mv, clock, j, ctl, ex = env
    tr.tcfg.entry_ttl_seconds = 60
    market.set("PEN", 1.00, 1.025)                    # 미체결로 남는 매수
    clock[0] = et(1, 0)
    tr.tick()
    calls = []
    ex.cancel = lambda oid: calls.append(oid) or "cancel-1"
    real_order = ex.order
    ex.order = lambda oid: {"order_id": oid, "status": "REJECTED", "filled": 0, "avg_price": None} \
        if oid == "cancel-1" else real_order(oid)
    clock[0] = et(1, 2)
    tr.tick()                                         # TTL 경과 → 취소 요청
    assert len(calls) == 1
    clock[0] = et(1, 4)
    tr.tick()                                         # 1분 넘게 처리 중 → 취소 주문 확인 → 거부 → 재시도 가능
    tr.tick()
    assert len(calls) == 2


def test_scale_out_then_trailing_stop(env):
    """익절가에서 절반 매도 → 나머지는 본전 손절 + 고점 대비 −8% 트레일링, '매도 구간'에도 팔지 않는다."""
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    qty, entry = t["qty"], t["entry_price"]
    assert t["scale_out"] == 0.5 and t["trail_pct"] == 8.0
    clock[0] = et(5, 0)
    market.set("PEN", 1.20, 1.21)                     # +16% > 익절 12%
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["scaled"] == 1 and t["remaining"] == qty - qty // 2 and t["status"] == "OPEN"
    assert t["stop"] == entry                          # 나머지는 본전 손절
    clock[0] = et(9, 5)                                # 매도 구간이지만 트레일링 중 → 유지
    market.set("PEN", 1.40, 1.41)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "OPEN" and t["high_water"] == 1.40
    clock[0] = et(9, 40)
    market.set("PEN", 1.28, 1.29)                      # 고점 1.40 × 0.92 = 1.288 아래
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"].startswith("익절(분할) + 트레일링 스톱")
    assert t["exited_qty"] == qty and t["pnl"] > 0


def test_no_scale_when_full_take_profit(env):
    tr, market, mv, clock, j, ctl, ex = env
    tr.tcfg.strategies["runner_overnight"]["scale_out_pct"] = 1.0
    tr.strategies["runner_overnight"].p = tr.tcfg.strategies["runner_overnight"]
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    clock[0] = et(5, 0)
    market.set("PEN", 1.20, 1.21)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "익절"
