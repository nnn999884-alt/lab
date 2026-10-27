from datetime import date, datetime, timezone

from pennybot.collector import Collector, Store
from pennybot.config import CollectorConfig
from pennybot.news import Item, classify_news, parse_feed, tickers
from pennybot.sessions import SessionCalendar
from pennybot.trading.market import MarketView

D = date(2026, 10, 21)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>ACME Announces Pricing of $5 Million Registered Direct Offering</title>
<link>https://x.example/1</link><guid>g1</guid>
<description>&lt;p&gt;ACME Corp. (NASDAQ: ACME) today announced the pricing...&lt;/p&gt;</description>
<pubDate>Wed, 21 Oct 2026 21:05:00 -0400</pubDate></item>
<item><title>BioZ Receives FDA Clearance for Device</title><link>https://x.example/2</link>
<description>BioZ Inc. (Nasdaq: BIOZ) announced FDA 510(k) clearance</description>
<pubDate>Wed, 21 Oct 2026 22:00:00 -0400</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Zapp secures $12 million purchase order from utility</title><link href="https://y.example/a"/>
<id>a1</id><updated>2026-10-22T01:30:00Z</updated><summary>Zapp Ltd. (NYSE American: ZAPP)</summary></entry>
</feed>"""


def test_parse_rss_and_atom_and_tickers():
    items = parse_feed(RSS) + parse_feed(ATOM)
    assert [i.id for i in items] == ["g1", "https://x.example/2", "a1"]
    assert items[0].published.utcoffset().total_seconds() == -4 * 3600
    assert tickers(items[0].summary) == {"ACME"}
    assert tickers("Zapp Ltd. (NYSE American: ZAPP) and Foo (OTCQB: FOOO)") == {"ZAPP"}


def test_classify_news_direction():
    kinds = lambda evs: {(e.kind, e.direction) for e in evs}
    assert ("PUBLIC_OFFERING", -1) in kinds(classify_news("ACME Announces Pricing of $5 Million Offering", "", D))
    assert kinds(classify_news("BioZ Receives FDA Clearance", "", D)) == {("FDA_APPROVAL", 1)}
    assert ("MATERIAL_NEWS", 1) in kinds(classify_news("Zapp secures $12 million purchase order", "", D))
    # 공모 기사 안의 'agreement' 로 호재가 같이 잡혀도 악재만 남긴다
    evs = classify_news("ACME prices registered direct offering under securities purchase agreement",
                        "partnership agreement", D)
    assert all(e.direction < 0 for e in evs)
    assert ("ATM_TERMINATED", 1) in kinds(classify_news("Nano terminates at-the-market sales agreement", "", D))
    assert ("DELISTING_NOTICE", -1) in kinds(classify_news("Lowq receives notification letter from Nasdaq", "", D))


class FakeNews:
    def fetch(self):
        return [("globenewswire.com", i) for i in parse_feed(RSS)] + [
            ("x", Item("dup", "Other (NASDAQ: OTHR) news", "", "", datetime(2026, 10, 22, 2, tzinfo=timezone.utc)))]


class Toss:
    def market_calendar_us(self, date=None):
        return {}


def test_collector_stores_news_for_universe_and_market_view_merges(tmp_path):
    store = Store(tmp_path / "m.db")
    col = Collector(Toss(), store, SessionCalendar(), CollectorConfig(), None, news=FakeNews())
    col.universe = {"ACME": {}, "BIOZ": {}}
    col.poll_news(datetime(2026, 10, 22, 3, tzinfo=timezone.utc))
    rows = store.query("SELECT symbol, session, trade_date, events, direction FROM news ORDER BY symbol")
    # 21:05 ET 공개 → 다음 영업일(10/22) 데이마켓
    assert rows == [("ACME", "DAY", "2026-10-22", "PUBLIC_OFFERING,REGISTERED_DIRECT", -1),
                    ("BIOZ", "DAY", "2026-10-22", "FDA_APPROVAL", 1)]
    col.poll_news(datetime(2026, 10, 22, 3, 5, tzinfo=timezone.utc))      # 같은 기사는 다시 넣지 않음
    assert store.query("SELECT COUNT(*) FROM news")[0][0] == 2
    mv = MarketView(str(tmp_path / "m.db"), None, SessionCalendar(), None,
                    clock=lambda: datetime(2026, 10, 22, 3, tzinfo=timezone.utc))
    since = datetime(2026, 10, 21, 20, tzinfo=timezone.utc)
    got = {(f["symbol"], f["form"], f["events"]) for f in mv.filings_since(since)}
    assert ("BIOZ", "NEWS", "FDA_APPROVAL") in got
    assert mv.filings_for("ACME", 3, datetime(2026, 10, 22, 3, tzinfo=timezone.utc))[0]["form"] == "NEWS"
    # 백테스트 as-of: 공개 전 시각에서는 보이지 않아야 한다
    mv_past = MarketView(str(tmp_path / "m.db"), None, SessionCalendar(), None,
                         clock=lambda: datetime(2026, 10, 22, 1, tzinfo=timezone.utc))
    assert not mv_past.filings_since(since)


def test_recent_flow_reads_realtime_kinds_since_cutoff(tmp_path):
    """수집기가 실시간 체결로 쓴 거래 터짐/애프터 누적/바닥 재반등/+20% 돌파 이벤트를 단타 전략이 읽을 수 있어야 한다 —
    cutoff 이전 것과 다른 kind(공시 등)는 섞이면 안 되고, 최신 것이 먼저 와야 한다."""
    store = Store(tmp_path / "m.db")
    store.db.execute("INSERT INTO events (ts, symbol, kind, text, value) VALUES "
                      "('2026-10-22T03:00:00+00:00', 'OLD', 'burst', '옛날 신호', 10)")
    store.db.execute("INSERT INTO events (ts, symbol, kind, text, value) VALUES "
                      "('2026-10-22T03:05:00+00:00', 'BRS', 'burst', '거래 터짐', 12.5)")
    store.db.execute("INSERT INTO events (ts, symbol, kind, text, value) VALUES "
                      "('2026-10-22T03:06:00+00:00', 'BTM', 'bottom_bounce', '바닥 재반등', 16.0)")
    store.db.execute("INSERT INTO events (ts, symbol, kind, text, value) VALUES "
                      "('2026-10-22T03:06:30+00:00', 'OTH', 'filing', '관련없는 공시', 0)")
    store.db.commit()
    mv = MarketView(str(tmp_path / "m.db"), None, SessionCalendar(), None,
                    clock=lambda: datetime(2026, 10, 22, 3, 7, tzinfo=timezone.utc))
    got = mv.recent_flow(since=datetime(2026, 10, 22, 3, 4, tzinfo=timezone.utc))
    assert [g["symbol"] for g in got] == ["BTM", "BRS"]   # OLD 는 cutoff 전, OTH 는 kind 가 다름, 최신 순
    assert got[0] == {"symbol": "BTM", "kind": "bottom_bounce", "ts": "2026-10-22T03:06:00+00:00",
                      "text": "바닥 재반등", "chg": 16.0}
