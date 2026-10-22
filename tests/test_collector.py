from datetime import date, datetime, timedelta

from pennybot.collector import Collector, Store
from pennybot.config import CollectorConfig
from pennybot.screener import edgar as ed
from pennybot.sessions import AFTER, CLOSED, DAY, ET, KST, PRE, REGULAR, SessionCalendar, parse_api_day

D = date(2026, 10, 21)   # 수요일, 서머타임 (EDT)

API_DAY = {   # 토스 캘린더 형식 (KST)
    "date": "2026-10-21",
    "dayMarket": {"startTime": "2026-10-21T09:00:00+09:00", "endTime": "2026-10-21T17:00:00+09:00"},
    "preMarket": {"startTime": "2026-10-21T17:00:00+09:00", "endTime": "2026-10-21T22:30:00+09:00"},
    "regularMarket": {"startTime": "2026-10-21T22:30:00+09:00", "endTime": "2026-10-22T05:00:00+09:00"},
    "afterMarket": {"startTime": "2026-10-22T05:00:00+09:00", "endTime": "2026-10-22T08:50:00+09:00"},
}


API_NEXT = {   # 다음 영업일 10/22: 데이마켓은 10/21 20:00 ET (= 10/22 09:00 KST) 시작
    "date": "2026-10-22",
    "dayMarket": {"startTime": "2026-10-22T09:00:00+09:00", "endTime": "2026-10-22T17:00:00+09:00"},
    "preMarket": {"startTime": "2026-10-22T17:00:00+09:00", "endTime": "2026-10-22T22:30:00+09:00"},
    "regularMarket": {"startTime": "2026-10-22T22:30:00+09:00", "endTime": "2026-10-23T05:00:00+09:00"},
    "afterMarket": {"startTime": "2026-10-23T05:00:00+09:00", "endTime": "2026-10-23T08:50:00+09:00"},
}


def et(h, m, d=D):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def test_static_and_api_calendar_agree():
    static = SessionCalendar()
    api = SessionCalendar()
    api.add(parse_api_day(API_DAY))
    for ts, want in [(et(20, 30, D - timedelta(days=1)), DAY), (et(5, 0), PRE), (et(10, 0), REGULAR),
                     (et(17, 0), AFTER)]:
        assert static.classify(ts) == (D, want)
        assert api.classify(ts, auto_refresh=False) == (D, want)
    assert static.classify(et(19, 55))[1] == CLOSED          # 애프터 종료(19:50) ~ 데이마켓(20:00) 사이
    assert static.classify(datetime(2026, 10, 24, 12, tzinfo=ET)) == (None, CLOSED)   # 토요일
    # 데이마켓 시작 = 한국 오전 9시 (서머타임)
    assert static.bounds(D, DAY)[0].astimezone(KST).hour == 9


def test_parse_current_atom():
    xml = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>4 - ACME CORP (0001234567) (Issuer)</title>
<updated>2026-10-21T16:32:11-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000950170-26-000111</id></entry>
<entry><title>4 - Doe Jane (0007654321) (Reporting)</title>
<updated>2026-10-21T16:32:11-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000950170-26-000111</id></entry>
<entry><title>8-K - ACME CORP (0001234567) (Filer)</title>
<updated>2026-10-21T21:05:00-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0001234567-26-000222</id></entry>
</feed>"""
    rows = ed.parse_current_atom(xml)
    assert [(r["form"], r["cik"], r["role"]) for r in rows] == [
        ("4", 1234567, "Issuer"), ("4", 7654321, "Reporting"), ("8-K", 1234567, "Filer")]
    assert rows[2]["accepted"].utcoffset() == timedelta(hours=-4)


class FakeToss:
    def __init__(self):
        self.prices_calls = 0
        self.candle_pages = []

    def market_calendar_us(self, date=None):
        return {"today": API_DAY, "previousBusinessDay": API_DAY, "nextBusinessDay": API_NEXT}

    def stocks_all(self, market):
        return {"NASDAQ": [{"symbol": "PEN", "name": "페니"}, {"symbol": "BIG", "name": "대형"}],
                "NYSE": [{"symbol": "LOW", "name": "저가"}]}.get(market, [])

    def prices(self, symbols):
        self.prices_calls += 1
        px = {"PEN": "1.20", "BIG": "180.00", "LOW": "0.50"}
        return [{"symbol": s, "lastPrice": px[s], "timestamp": None} for s in symbols if s in px]

    def orderbook(self, symbol):
        if symbol == "LOW":
            return {"bids": [], "asks": []}     # 데이마켓에서 호가가 없는 경우
        return {"timestamp": None, "bids": [{"price": "1.19", "volume": "100"}],
                "asks": [{"price": "1.21", "volume": "200"}]}

    def rankings(self, type_, duration="realtime", **kw):
        return {"rankedAt": "x", "rankings": [{"rank": 1, "symbol": "BIG", "price": {"lastPrice": "180",
                "basePrice": "170", "changeRate": "0.05"}, "tradingVolume": "1", "tradingAmount": "1"},
                {"rank": 2, "symbol": "PEN", "price": {"lastPrice": "1.2", "basePrice": "1",
                 "changeRate": "0.2"}, "tradingVolume": "1", "tradingAmount": "1"}]}

    def candles(self, symbol, interval, count, before=None):
        # 데이마켓 2봉 + 정규장 1봉, 최신순. 두 번째 페이지 없음
        ts = ["2026-10-21T10:01:00-04:00", "2026-10-21T03:59:00-04:00", "2026-10-21T03:58:00-04:00"]
        return {"candles": [{"timestamp": t, "openPrice": "1", "highPrice": "1.3", "lowPrice": "0.9",
                             "closePrice": "1.2", "volume": "10"} for t in ts], "nextBefore": None}


class FakeEdgar:
    def ticker_map(self):
        return [{"cik": 1234567, "ticker": "LOW", "exchange": "NYSE"}]

    def current(self, form):
        if form != "4":
            return []
        return [{"form": "4", "cik": 1234567, "role": "Issuer", "accession": "acc-1",
                 "accepted": et(21, 0)},    # 장 마감 후 21:00 ET → 다음 영업일 데이마켓
                {"form": "4", "cik": 7654321, "role": "Reporting", "accession": "acc-1",
                 "accepted": et(21, 0)}]

    def filings(self, cik):
        return [ed.Filing("4", D, "acc-1", "xslF345X05/f4.xml")]

    def form4_xml(self, cik, f):
        return ("<ownershipDocument><nonDerivativeTable><nonDerivativeTransaction>"
                "<transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
                "<transactionAmounts><transactionShares><value>20000</value></transactionShares>"
                "<transactionPricePerShare><value>0.5</value></transactionPricePerShare>"
                "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
                "</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>")


def make(tmp_path, now):
    toss = FakeToss()
    clock = [now]
    store = Store(tmp_path / "m.db")
    cal = SessionCalendar(toss)
    col = Collector(toss, store, cal, CollectorConfig(min_price=0.2, max_price=10), FakeEdgar(),
                    clock=lambda: clock[0])
    return col, toss, store, clock


def test_collector_tick_tags_sessions(tmp_path):
    col, toss, store, clock = make(tmp_path, et(1, 0))   # 데이마켓 중 (한국 14:00)
    col.tick()
    assert set(col.universe) == {"PEN", "LOW"}            # $180 대형주 제외
    assert store.query("SELECT DISTINCT session, trade_date FROM prices") == [("DAY", "2026-10-21")]
    assert set(col.watch) == {"PEN", "LOW"}               # 랭킹의 BIG 은 유니버스 밖이라 제외
    q = dict(store.query("SELECT symbol, bid FROM quotes"))
    assert q["PEN"] == 1.19 and q["LOW"] is None           # 빈 호가도 기록
    bars = store.query("SELECT session, COUNT(*) FROM bars_1m GROUP BY session ORDER BY session")
    assert bars == [("DAY", 4), ("REGULAR", 2)]            # 봉 종료 시각 기준 → 봉 중간으로 판정
    f = store.query("SELECT symbol, session, trade_date, insider_buy_usd FROM filings")
    # 장 마감 후 21:00 ET 접수 → 다음 영업일(10/22)의 데이마켓에 공개된 공시
    assert f == [("LOW", "DAY", "2026-10-22", 10000.0)]

    # 1분봉은 이미 받은 시각 이후만 다시 받는다
    n_before = store.query("SELECT COUNT(*) FROM bars_1m")[0][0]
    col.sync_candles(et(1, 0))
    assert store.query("SELECT COUNT(*) FROM bars_1m")[0][0] == n_before


def test_session_change_triggers_summary(tmp_path):
    col, toss, store, clock = make(tmp_path, et(19, 0))   # 애프터마켓
    col.tick()
    clock[0] = et(19, 55)                                  # 애프터 종료 → CLOSED
    col.tick()
    rows = store.query("SELECT symbol, session, quotes FROM session_summary WHERE trade_date='2026-10-21'")
    assert ("PEN", "AFTER", 1) in rows
    calls = toss.prices_calls
    col.tick()                                             # 휴장 중에는 시세를 부르지 않는다
    assert toss.prices_calls == calls


def test_calendar_refresh_called_once_per_date():
    class Counting(FakeToss):
        n = 0

        def market_calendar_us(self, date=None):
            Counting.n += 1
            return super().market_calendar_us(date)

    cal = SessionCalendar(Counting())
    old = et(12, 0, D - timedelta(days=30))     # 캘린더 범위 밖의 오래된 봉
    for _ in range(50):
        cal.classify(old)
    assert Counting.n == 1


def test_record_runners_with_dilution_profile(tmp_path):
    from pennybot.dilution import Profile

    class Fund:
        def shares(self, s, d): return 2_000_000
        def profile(self, s, px, d): return Profile(score=15, bucket="low", recent_positive=["ATM_TERMINATED"])
        def flags(self, s, d, has_cat): return [] if has_cat else ["공시·촉매 없는 급등"]

    class Toss(FakeToss):
        def candles(self, symbol, interval, count, before=None):
            if interval == "1d":
                return {"candles": [{"timestamp": "2026-10-21T00:00:00-04:00", "openPrice": "0.8",
                                     "highPrice": "1.30", "lowPrice": "0.75", "closePrice": "1.25",
                                     "volume": "6000000"}]}
            return super().candles(symbol, interval, count, before)

    store = Store(tmp_path / "m.db")
    col = Collector(Toss(), store, SessionCalendar(Toss()), CollectorConfig(), None, fundamentals=Fund())
    rows = []
    for d, pen, flat in [("2026-10-20", 0.80, 2.0), ("2026-10-21", 1.25, 2.1)]:
        rows += [{"symbol": "PEN", "ts": f"{d}T19:59:00+00:00", "trade_date": d, "session": "REGULAR",
                  "last": pen, "price_ts": None},
                 {"symbol": "FLAT", "ts": f"{d}T19:59:00+00:00", "trade_date": d, "session": "REGULAR",
                  "last": flat, "price_ts": None}]
    store.put_prices(rows)
    col.record_runners("2026-10-21")
    r = store.query("SELECT symbol, round(change_pct), round(close_loc, 2), rotation, dilution_bucket, "
                    "dilution_reasons, pump_flags FROM runners")
    assert r == [("PEN", 56.0, 0.91, 3.0, "low", "ATM_TERMINATED", "공시·촉매 없는 급등")]
