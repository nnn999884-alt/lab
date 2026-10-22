import random
from datetime import date, datetime, timedelta, timezone

from pennybot.config import ScreenerConfig, StrategyConfig
from pennybot.models import Bar, Quote
from pennybot.screener import load_watchlist, run_screen
from pennybot.screener import edgar as ed
from pennybot.screener.factors import compute
from pennybot.screener.scorer import coil_scores, hard_filter, percentile_ranks
from pennybot.strategy import ET, PennyMomentumStrategy

TODAY = date(2026, 10, 21)


def daily(closes, vols, spread=0.04):
    t0 = datetime(2026, 4, 1, tzinfo=timezone.utc)
    bars = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = closes[i - 1] if i else c
        bars.append(Bar(t0 + timedelta(days=i), o, max(o, c) * (1 + spread / 2),
                        min(o, c) * (1 - spread / 2), c, v))
    return bars


def coiled():
    """60일 넓은 변동 후 20일 좁은 박스, 거래량은 증가, 종가는 고가권 마감."""
    rnd = random.Random(1)
    closes = [2.0 + rnd.uniform(-0.4, 0.4) for _ in range(70)]
    closes += [2.30 + 0.004 * i for i in range(20)]
    vols = [300_000] * 70 + [300_000 + 30_000 * i for i in range(20)]
    bars = daily(closes, vols, spread=0.10)
    for b in bars[-20:]:
        b.high, b.low = b.close * 1.01, b.close * 0.985   # 좁은 일중폭 + 고가 마감
    bars[-15].high = 2.40   # 박스 상단
    return bars


def noisy():
    rnd = random.Random(2)
    closes = [2.0 + rnd.uniform(-0.5, 0.5) for _ in range(90)]
    closes[-1] = 2.0
    vols = [600_000 - 4_000 * i for i in range(90)]
    return daily(closes, vols, spread=0.15)


def test_coil_features_rank_coiled_higher():
    fc, fn = compute(coiled()), compute(noisy())
    assert fc.atr_ratio < fn.atr_ratio
    assert fc.vol_trend > fn.vol_trend
    assert fc.clv > fn.clv
    s = coil_scores([fc, fn])
    assert s[0] > s[1]


def test_hard_filter():
    cfg = ScreenerConfig().filter_dict()
    f = compute(coiled())
    assert hard_filter(f, cfg) is None
    f.ret_5 = 0.8
    assert "급등" in hard_filter(f, cfg)


def test_percentile_ties():
    assert percentile_ranks([1, 2, 2, 3], True) == [0.0, 0.5, 0.5, 1.0]
    assert percentile_ranks([1, 3], False) == [1.0, 0.0]


def F(form, days_ago, items=""):
    return ed.Filing(form, TODAY - timedelta(days=days_ago), "0001-26-000001", "x.htm", items)


def test_edgar_dilution_shield():
    assert ed.evaluate([F("424B5", 10)], TODAY).blocked
    assert ed.evaluate([F("8-K", 20, "3.01,9.01")], TODAY).blocked
    assert ed.evaluate([F("NT 10-Q", 40)], TODAY).blocked
    r = ed.evaluate([F("S-3", 60), F("8-K", 50, "5.03"), F("8-K", 10, "3.02")], TODAY)
    assert not r.blocked and r.score == -50
    assert ed.evaluate([F("424B4", 200)], TODAY).score == 0   # 오래된 공시 무시


def test_edgar_catalysts():
    r = ed.evaluate([F("SC 13D", 5), F("8-K", 3, "1.01")], TODAY, form4_buys_usd=50_000)
    assert r.score == 8 + 3 + 12
    assert not r.blocked


FORM4 = """<?xml version="1.0"?><ownershipDocument><nonDerivativeTable>
<nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>10000</value></transactionShares>
<transactionPricePerShare><value>1.50</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
</nonDerivativeTransaction>
<nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>99999</value></transactionShares>
<transactionPricePerShare><value>1.60</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


def test_form4_insider_buy_parse():
    assert ed.insider_buy_usd(FORM4) == 15_000
    assert ed.insider_buy_usd("not xml") == 0


def test_parse_submissions():
    j = {"filings": {"recent": {"form": ["8-K", "4"], "filingDate": ["2026-10-01", "2026-10-02"],
                                "accessionNumber": ["a-1", "a-2"],
                                "primaryDocument": ["d.htm", "xslF345X05/f4.xml"],
                                "items": ["5.03", ""]}}}
    fs = ed.parse_submissions(j)
    assert fs[0].items == "5.03" and fs[1].form == "4"
    assert ed.recent_form4(fs, TODAY) == [fs[1]]


class FakeFeed:
    def __init__(self, data):
        self.data = data

    def get_daily_bars(self, symbol, days=180):
        if symbol not in self.data:
            raise KeyError(symbol)
        return self.data[symbol]


class FakeEdgar:
    def ticker_map(self):
        return [{"cik": 1, "ticker": "COIL", "exchange": "Nasdaq"},
                {"cik": 2, "ticker": "DILU", "exchange": "Nasdaq"},
                {"cik": 3, "ticker": "NOIS", "exchange": "NYSE"},
                {"cik": 4, "ticker": "OTCX", "exchange": "OTC"},
                {"cik": 5, "ticker": "GONE", "exchange": "Nasdaq"}]

    def filings(self, cik):
        return [F("424B5", 5)] if cik == 2 else [F("4", 3)] if cik == 1 else []

    def form4_xml(self, cik, f):
        return FORM4


def test_run_screen_end_to_end(tmp_path):
    feed = FakeFeed({"COIL": coiled(), "DILU": coiled(), "NOIS": noisy(), "OTCX": coiled()})
    cfg = ScreenerConfig(top_n=5, workers=2, min_dist_to_pivot=-1, max_dist_to_pivot=1)
    picks = run_screen(cfg, feed, FakeEdgar(), str(tmp_path), TODAY)
    syms = [p.symbol for p in picks]
    assert "DILU" not in syms          # 증자공시 차단
    assert "OTCX" not in syms          # OTC 제외
    assert syms[0] == "COIL"
    assert any("내부자" in r for r in picks[0].reasons)
    wl = load_watchlist(str(tmp_path), TODAY)
    assert wl["COIL"]["pivot"] > 0 and wl["COIL"]["avg_volume"] > 0
    assert (tmp_path / "screens" / f"{TODAY}.csv").exists()
    assert load_watchlist(str(tmp_path), TODAY + timedelta(days=5)) == {}


def test_pivot_entry():
    s = PennyMomentumStrategy(StrategyConfig())
    now = datetime(2026, 10, 21, 11, 0, tzinfo=ET)   # 개장 90분 → 페이스 기준 23%
    bars = [Bar(now - timedelta(minutes=1), 2.40, 2.46, 2.39, 2.45, 50_000)]
    q = Quote("COIL", 2.45, 2.445, 2.455, 400_000, 2.35)  # 평소 대비 4.3x
    sig = s.check_pivot_entry(q, bars, now, pivot=2.40, avg_volume=400_000)
    assert sig.ok, sig.reason
    assert abs(sig.stop_price - 2.40 * 0.97) < 1e-9
    low_vol = Quote("COIL", 2.45, 2.445, 2.455, 100_000, 2.35)
    assert "페이스" in s.check_pivot_entry(low_vol, bars, now, 2.40, 400_000).reason
    chase = Quote("COIL", 2.60, 2.595, 2.605, 400_000, 2.35)
    assert "과열" in s.check_pivot_entry(chase, bars, now, 2.40, 400_000).reason
