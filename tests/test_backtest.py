from datetime import date, datetime, timedelta, timezone

from pennybot.backtest import Backtest, ReplayMarket, apply_params, format_report
from pennybot.collector import Store
from pennybot.config import TradingConfig
from pennybot.sessions import ET, SessionCalendar

D, PREV = date(2026, 10, 21), date(2026, 10, 20)


def iso(ts):
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def price_at(ts):
    """데이마켓 1.03 → 프리 06:00 부터 상승 → 08:00 고점 1.40 → 09:00 이후 1.25 로 밀림."""
    h = ts.astimezone(ET)
    if h.date() == PREV or h.hour < 6:
        return 1.03
    if h.hour < 8:
        return 1.03 + (1.40 - 1.03) * ((h.hour - 6) * 60 + h.minute) / 120
    if h.hour < 9:
        return 1.40
    return 1.25


def build_db(path):
    s = Store(path)
    cal = SessionCalendar()
    s.put_runners([{"trade_date": PREV.isoformat(), "symbol": "PEN", "prev_close": 0.70, "close": 1.02,
                    "change_pct": 45.7, "high": 1.05, "low": 0.70, "volume": 4e6, "close_loc": 0.85}])
    s._put("session_summary", [{"symbol": "PEN", "trade_date": PREV.isoformat(), "session": "REGULAR",
                                "open": 0.72, "high": 1.05, "low": 0.70, "close": 1.02, "volume": 4e6}])
    prices, quotes = [], []
    ts = datetime(2026, 10, 20, 15, 0, tzinfo=ET)
    while ts < datetime(2026, 10, 21, 11, 0, tzinfo=ET):
        td, sess = cal.classify(ts)
        if td is None:                       # 19:50~20:00 ET 휴장
            ts += timedelta(minutes=1)
            continue
        p = round(price_at(ts), 4)
        prices.append({"symbol": "PEN", "ts": iso(ts), "trade_date": td.isoformat(), "session": sess,
                       "last": p, "price_ts": iso(ts)})
        quotes.append({"symbol": "PEN", "ts": iso(ts), "trade_date": td.isoformat(), "session": sess,
                       "bid": p, "bid_size": 5000, "ask": round(p * 1.004, 4), "ask_size": 5000, "levels": 5,
                       "book_ts": iso(ts)})
        ts += timedelta(minutes=1)
    s.put_prices(prices)
    s.put_quotes(quotes)
    # 미래(10/22)에 나올 증자 공시: 재생 시각에는 보이면 안 된다 (보이면 '급등 후 증자 공시'로 탈락)
    s.put_filings([{"accession": "f1", "cik": 1, "symbol": "PEN", "form": "424B5", "items": "",
                    "accepted": iso(datetime(2026, 10, 21, 12, 0, tzinfo=ET)), "trade_date": D.isoformat(),
                    "session": "REGULAR", "insider_buy_usd": None, "events": "TAKEDOWN", "direction": -1}])
    return path


def tcfg():
    t = TradingConfig(paper_cash=10_000)
    t.strategies["dilution_catalyst"]["enabled"] = False
    t.strategies["day_ignition"]["enabled"] = False
    t.strategies["runner_overnight"]["entry_discount_pct"] = 0.0   # 아래 테스트는 즉시 체결을 가정
    return t


def test_replay_runs_trader_without_lookahead(tmp_path):
    db = build_db(str(tmp_path / "m.db"))
    rep = Backtest(db, tcfg(), D, D, tmp_path / "bt", step_seconds=60, fee_pct=0.1).run()
    assert rep["all"]["trades"] == 1, format_report(rep)
    t = rep["trades"][0]
    assert t["symbol"] == "PEN" and t["strategy"] == "runner_overnight"
    # 익절가(+12%)에서 절반 → 나머지는 고점 1.40 의 −8% (1.288) 아래에서 트레일링 청산
    assert t["exit_reason"].startswith("익절(분할) + 트레일링 스톱")
    gross = rep["trades"][0]["net"]
    assert gross > 0 and rep["fee_pct"] == 0.1
    assert (tmp_path / "bt" / "report.json").exists()
    assert "검증 구간" in format_report(rep)


def test_replay_market_falls_back_to_last_with_assumed_spread(tmp_path):
    s = Store(tmp_path / "m.db")
    now = datetime(2026, 10, 21, 2, 0, tzinfo=ET)
    s.put_prices([{"symbol": "X", "ts": iso(now - timedelta(minutes=1)), "trade_date": "2026-10-21",
                   "session": "DAY", "last": 2.0, "price_ts": None}])
    m = ReplayMarket(str(tmp_path / "m.db"), lambda: now, half_spread_pct=1.0, assumed_size=500)
    b = m.book("X")
    assert (round(b.bid, 4), round(b.ask, 4), b.ask_size) == (1.98, 2.02, 500)
    later = ReplayMarket(str(tmp_path / "m.db"), lambda: now + timedelta(minutes=10))
    assert not later.book("X").has_quote          # 오래된 가격으로는 체결하지 않는다
    early = ReplayMarket(str(tmp_path / "m.db"), lambda: now - timedelta(minutes=5))
    assert early.book("X").last == 0              # 미래 가격은 안 보인다


def test_apply_params():
    t = tcfg()
    got = apply_params(t, ["runner_overnight.take_profit_pct=15", "max_open_positions=4"])
    assert t.strategies["runner_overnight"]["take_profit_pct"] == 15 and t.max_open_positions == 4
    assert got == {"runner_overnight.take_profit_pct": 15, "max_open_positions": 4}
