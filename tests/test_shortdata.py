from datetime import date, datetime, timezone

from pennybot.brief import brief, render, watch_shorts
from pennybot.collector import Store
from pennybot.collector.runner import Collector
from pennybot.config import CollectorConfig
from pennybot.shortdata import parse_finra, parse_halts, parse_ibkr, parse_ssr, parse_threshold

FINRA_TXT = """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20261022|PENX|600000.5|100|1000000.25|B,Q,N
20261022|AAPL|493721|7862|997855|B,Q,N
"""
IBKR_TXT = """#BOF|2026.10.22|17:46:34
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
049323AB4|USD|SOME BOND|557991763|XXX|3.6300|0.2500|300000|BBG|
PENX|USD|PENX INC|1|US1|-40.1|45.5|>10000000|BBG|
LOWQ|USD|LOWQ INC|2|US2|3.2|0.25|4500|BBG|
#EOF|3
"""
SSR_TXT = """Symbol,Security Name,Market Category,Trigger Time
PENX,"Penx, Inc.",Q,10/21/2026 9:30:00 AM
"""
TH_TXT = """Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler
PENX|PENX INC|G|Y|N|
LOWQ|LOWQ INC|G|N|N|
"""
HALTS_XML = """﻿<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
<item><title>PENX</title><ndaq:HaltDate>10/22/2026</ndaq:HaltDate><ndaq:HaltTime>10:41:02.000</ndaq:HaltTime>
<ndaq:IssueSymbol>PENX</ndaq:IssueSymbol><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
<ndaq:ResumptionDate>10/22/2026</ndaq:ResumptionDate><ndaq:ResumptionTradeTime>10:46:02.000</ndaq:ResumptionTradeTime></item>
<item><title>NEWS</title><ndaq:HaltDate>10/22/2026</ndaq:HaltDate><ndaq:HaltTime>16:01:00.000</ndaq:HaltTime>
<ndaq:IssueSymbol>NEWS</ndaq:IssueSymbol><ndaq:ReasonCode>T1</ndaq:ReasonCode><ndaq:ResumptionDate /></item>
</channel></rss>"""


def test_parsers():
    f = parse_finra(FINRA_TXT)
    assert f[0] == {"trade_date": "2026-10-22", "symbol": "PENX", "short_volume": 600000.5, "exempt_volume": 100.0,
                    "total_volume": 1000000.25}
    stamp, b = parse_ibkr(IBKR_TXT)
    assert stamp == "2026-10-22T17:46:34"
    assert [r["symbol"] for r in b] == ["PENX", "LOWQ"]                 # 채권 CUSIP 은 빠짐
    assert b[0]["available"] == 10_000_000 and b[0]["fee_rate"] == 45.5
    assert parse_ssr(SSR_TXT, date(2026, 10, 22)) == [{"trade_date": "2026-10-22", "symbol": "PENX",
                                                       "trigger_time": "10/21/2026 9:30:00 AM"}]
    assert parse_threshold(TH_TXT, date(2026, 10, 22)) == [{"trade_date": "2026-10-22", "symbol": "PENX"}]
    h = parse_halts(HALTS_XML)
    assert h[0]["symbol"] == "PENX" and h[0]["reason_ko"] == "변동성 정지(LULD)"
    assert h[0]["halt_ts"].startswith("2026-10-22T10:41:02-04:00") and h[0]["resume_ts"].startswith("2026-10-22T10:46:02")
    assert h[1]["resume_ts"] is None and h[1]["reason_ko"] == "뉴스 대기"


class FakeShort:
    def __init__(self):
        self.calls = 0

    def recent_days(self, now, n=5):
        return [date(2026, 10, 22), date(2026, 10, 21)]

    def finra(self, day):
        self.calls += 1
        return parse_finra(FINRA_TXT) if day == date(2026, 10, 22) else []    # 21일 파일은 아직 없음

    def ssr(self, day):
        return parse_ssr(SSR_TXT, day) if day == date(2026, 10, 22) else []

    def threshold(self, day):
        return parse_threshold(TH_TXT, day) if day == date(2026, 10, 22) else []

    def borrow(self):
        return parse_ibkr(IBKR_TXT)

    def halts(self):
        return parse_halts(HALTS_XML)


def test_collector_stores_universe_only_and_skips_done_days(tmp_path):
    st = Store(tmp_path / "m.db")
    now = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
    sh = FakeShort()
    col = Collector(None, st, None, CollectorConfig(db_path=str(tmp_path / "m.db")), short=sh, clock=lambda: now)
    col.universe = {"PENX": {}, "LOWQ": {}}
    col.poll_short_daily(now)
    col.poll_borrow(now)
    col.poll_halts()
    col.poll_halts()                                                      # 같은 정지는 한 번만
    assert st.query("SELECT symbol FROM short_volume") == [("PENX",)]     # AAPL 은 유니버스 밖
    assert st.query("SELECT symbol FROM ssr") == [("PENX",)] and st.query("SELECT symbol FROM reg_sho") == [("PENX",)]
    assert {r[0] for r in st.query("SELECT symbol FROM borrow")} == {"PENX", "LOWQ"}
    assert st.query("SELECT symbol, reason FROM halts") == [("PENX", "LUDP")]
    n = sh.calls
    col.poll_short_daily(now)
    assert sh.calls == n + 1          # 받은 날(22일)은 다시 안 받고, 아직 없던 21일만 다시 시도

    st.put_prices([{"symbol": "PENX", "ts": "2026-10-22T13:00:00+00:00", "trade_date": "2026-10-21", "session": "REGULAR",
                    "last": 1.0, "price_ts": None},
                   {"symbol": "PENX", "ts": "2026-10-23T00:30:00+00:00", "trade_date": "2026-10-22", "session": "DAY",
                    "last": 1.2, "price_ts": None}])
    b = brief(str(tmp_path / "m.db"), "penx", now)
    assert b["found"] and abs(b["change_pct"] - 20) < 1e-9 and abs(b["short"]["ratio"] - 0.6) < 1e-6
    assert b["borrow"]["fee_rate"] == 45.5 and b["ssr"] and b["reg_sho"] and b["halts"][0]["reason"] == "LUDP"
    text = render(b)
    assert text.startswith("🔎 PENX") and "공매도 비율 60%" in text and "대차 수수료 45.5%" in text and "SSR" in text
    assert "변동성 정지" in text
    assert not brief(str(tmp_path / "m.db"), "ZZZZ", now)["found"]
    w = {r["symbol"]: r for r in watch_shorts(str(tmp_path / "m.db"), ["PENX", "LOWQ"])}
    assert w["PENX"]["ssr"] and w["LOWQ"]["available"] == 4500 and w["LOWQ"]["short_ratio"] is None
