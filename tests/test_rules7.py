"""rules7 전략 7개 중 CatalystPullback(호재·내부자 눌림) 스캔 테스트."""
from datetime import datetime, timedelta, timezone

from pennybot.config import TradingConfig
from pennybot.trading.exchange import Book
from pennybot.trading.rules7 import CatalystPullback, OpenVWAPHold, hard_check, pass_score
from pennybot.trading.strategies import Ctx, quote_check


NOW = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)
DAY = NOW.date()
TD = TradingConfig()


class Cal:
    def bounds(self, d, s):
        return (NOW - timedelta(days=2), NOW)


class FakeMV:
    def __init__(self, books=None, sql=None, closes=None, filings=(), recent=None):
        self.books = books or {}
        self.sql = sql or []
        self.closes = closes or {}
        self.filings = list(filings)
        self.recent = dict(recent or {})

    def q(self, sql, args=()):
        for key, rows in self.sql:
            if key in sql:
                return rows
        return []

    def book(self, sym):
        return self.books.get(sym, Book(sym, 0, 0, 0, 0, 0))

    def prev_close(self, sym, d, allow_api=True):
        return self.closes.get(sym)

    def filings_since(self, since):
        return [f for f in self.filings if f["accepted"] >= since.astimezone(timezone.utc).isoformat()]

    def filings_for(self, sym, days, now):
        return self.recent.get(sym, [])

    def prev_trade_date(self, d):
        return d - timedelta(days=1)

    def clock(self):
        return NOW

    def _asof(self):
        return NOW.isoformat()


def ctx(mv, session="REGULAR", held=None):
    return Ctx(NOW, DAY, session, Cal(), mv, TD, held=held or set())


def filing(sym, form="8-K", events="MATERIAL_NEWS", direction=1, accepted=None, insider_buy_usd=0):
    return {
        "symbol": sym,
        "form": form,
        "accepted": (accepted or NOW).isoformat(),
        "events": events,
        "direction": direction,
        "insider_buy_usd": insider_buy_usd,
    }


def bars(n=10, high_move=20.0, pull=4.0, vol_rally=1000.0, vol_pull=400.0):
    """첫 절반은 고점까지 상승,後半은 pull% 만큼 눌림. hi_i = n//2 - 1."""
    rows = []
    hi_i = n // 2 - 1
    for i in range(n):
        if i <= hi_i:
            frac = i / hi_i if hi_i > 0 else 1.0
            h = 1.0 * (1 + frac * high_move / 100)
            c = h * 0.998
            vol = vol_rally
        else:
            frac = (i - hi_i) / (n - hi_i - 1) if n - hi_i - 1 > 0 else 1.0
            h = 1.0 * (1 + high_move / 100) * (1 - frac * pull / 100)
            c = h * 0.998
            vol = vol_pull
        rows.append((h, h * 0.995, c, vol))
    return rows


def test_open_vwap_hold_uses_its_regular_session_exit_time():
    class VWAPMarket(FakeMV):
        def q(self, sql, args=()):
            if "SUM((high+low+close)/3*volume)" in sql:
                return [(1200.0, 1000.0)]
            if "SELECT high FROM bars_1m" in sql:
                return [(1.3,)]
            if "SELECT high, low, close, volume" in sql:
                return [(1.3, 1.25, 1.27, 100)] * 6
            return []

    mv = VWAPMarket(books={"PEN": Book("PEN", 1.29, 1.30, 1000, 1000, 1.295)},
                    closes={"PEN": 1.0}, filings=[filing("PEN")])
    plans = OpenVWAPHold(TD.strategies["open_vwap_hold"]).scan(ctx(mv))
    assert len(plans) == 1
    assert plans[0].sell_from == ctx(mv).regular_open + timedelta(minutes=55)


# ---------- pass_score / hard_check ----------

def test_pass_score():
    ok, txt = pass_score({"a": True, "b": False, "c": True}, 2)
    assert ok and txt.startswith("a·c") and "(2/3)" in txt
    assert not pass_score({"a": True, "b": False}, 2)[0]


def test_missing_quote_reports_actual_session():
    from types import SimpleNamespace
    market = FakeMV()
    for session, label in (("DAY", "데이마켓"), ("PRE", "프리마켓"), ("REGULAR", "정규장")):
        _, reason = quote_check("PEN", SimpleNamespace(mv=market, session=session, tcfg=TD))
        assert reason == f"{label} 호가 없음"


def test_hard_check():
    from types import SimpleNamespace
    mv = FakeMV(sql=[("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=?", [(50_000.0,)])])
    c = SimpleNamespace(tcfg=TD, mv=mv, trade_date=DAY, now=NOW)
    assert hard_check(c, "PEN", 1.0) is None
    assert "가격" in hard_check(c, "PEN", 0.2) and "가격" in hard_check(c, "PEN", 20.0)
    wide = FakeMV(books={"PEN": Book("PEN", 1.00, 1.06, 10_000, 10_000, 1.03)}, sql=mv.sql)
    assert "스프레드" in hard_check(SimpleNamespace(tcfg=TD, mv=wide, trade_date=DAY, now=NOW), "PEN", 1.0)
    thin = FakeMV(books=mv.books, sql=[(mv.sql[0][0], [(10_000.0,)])])
    assert "5분 거래대금" in hard_check(SimpleNamespace(tcfg=TD, mv=thin, trade_date=DAY, now=NOW), "PEN", 1.0)
    off = FakeMV(books=mv.books, sql=mv.sql,
                 recent={"PEN": [{"form": "424B5", "events": "PUBLIC_OFFERING", "accepted": NOW.isoformat()}]})
    assert "오퍼링" in hard_check(SimpleNamespace(tcfg=TD, mv=off, trade_date=DAY, now=NOW), "PEN", 1.0)


# ---------- CatalystPullback ----------

def _catalyst_mv(sym="PEN", pc=1.00, high_move=20.0, pull=4.0, vol_rally=1000.0, vol_pull=400.0,
                 book=None, form="8-K", events="MATERIAL_NEWS", direction=1, insider_buy_usd=0,
                 vwap_num=1100.0, vwap_den=1000.0):
    bs = bars(n=10, high_move=high_move, pull=pull, vol_rally=vol_rally, vol_pull=vol_pull)
    if book is None:
        if pc is None:
            b = Book(sym, 0, 0, 0, 0, 0)
        else:
            mid = pc * (1 + (high_move - pull) / 100)
            b = Book(sym, mid * 0.995, mid * 1.005, 10_000, 10_000, mid)
    else:
        b = book
    sql = [
        ("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts", bs),
        ("SUM((high+low+close)/3*volume), SUM(volume) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?",
         [(vwap_num, vwap_den)]),
    ]
    closes = {sym: pc} if pc is not None else {}
    return FakeMV(books={sym: b}, sql=sql, closes=closes,
                  filings=[filing(sym, form=form, events=events, direction=direction,
                                  insider_buy_usd=insider_buy_usd)])


def test_catalyst_pullback_basic_pass():
    """호재 공시 + 당일 +20% + 고점 −4% 눌림 + VWAP 위 + 눌림 거래량 감소 → 매수 시도."""
    mv = _catalyst_mv()
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    ok = [p for p in plans if p.ok]
    assert [p.symbol for p in ok] == ["PEN"]
    assert ok[0].stop == ok[0].price * (1 - 12.0 / 100)
    assert ok[0].target == ok[0].price * (1 + 10.0 / 100)


def test_catalyst_pullback_skips_held():
    mv = _catalyst_mv()
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv, held={"PEN"}))
    assert plans == []


def test_catalyst_pullback_no_prev_close():
    mv = _catalyst_mv(pc=None)
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    assert plans == []


def test_catalyst_pullback_few_bars():
    mv = _catalyst_mv()
    mv.sql = [("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts",
               bars(n=3, high_move=20.0, pull=4.0))]
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    assert plans == []


def test_catalyst_pullback_move_too_small():
    mv = _catalyst_mv(high_move=1.0, pull=4.0)
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    assert plans == []


def test_catalyst_pullback_pull_too_large():
    mv = _catalyst_mv(high_move=20.0, pull=20.0)
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    assert plans == []


def test_catalyst_pullback_bad_quote():
    mv = _catalyst_mv(book=Book("PEN", 0, 0, 0, 0, 0))
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    assert len(plans) == 1 and not plans[0].ok


def test_catalyst_pullback_score_fail():
    """VWAP 아래 + 눌림 거래량 증가 → 점수 2개 중 0개 → 거절."""
    mv = _catalyst_mv(vol_rally=400.0, vol_pull=1000.0, vwap_num=2000.0, vwap_den=1000.0)
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    assert len(plans) == 1 and not plans[0].ok


def test_catalyst_pullback_form4_insider_buy():
    """Form 4 내부자 장내매수($5천↑)도 촉매로 인정."""
    mv = _catalyst_mv(form="4", events="", insider_buy_usd=6000.0)
    plans = CatalystPullback(TD.strategies["catalyst_pullback"]).scan(ctx(mv))
    ok = [p for p in plans if p.ok]
    assert [p.symbol for p in ok] == ["PEN"]
