from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pennybot.dilution import Event
from pennybot.motive import motives
from pennybot.research import ResearchDB, factor_table, find_runners, simulate
from pennybot.screener.edgar import Filing

ET = ZoneInfo("America/New_York")
T = date(2026, 9, 15)


def ev(kind, d, direction=-1):
    return Event(kind, direction, "8-K", d, "")


def keys(ms):
    return {m.key for m in ms}


def test_delisting_deadline_until_compliance_regained():
    ms = motives([ev("DELISTING_NOTICE", date(2026, 6, 1))], [], None, T, 0.6)
    m = next(x for x in ms if x.key == "delisting")
    assert m.days == (date(2026, 6, 1) + timedelta(days=180) - T).days and m.label.startswith("상폐 기한 D-")
    ms = motives([ev("DELISTING_NOTICE", date(2026, 6, 1)), ev("COMPLIANCE_REGAINED", date(2026, 8, 1), 1)], [], None, T, 1.2)
    assert "delisting" not in keys(ms)
    # 미래 공시는 안 본다 (as-of)
    assert "delisting" not in keys(motives([ev("DELISTING_NOTICE", date(2026, 9, 20))], [], None, T, 0.6))


def test_atm_offering_warrants_cash_split_meeting_periodic():
    evs = [ev("ATM_NEW", date(2026, 8, 1)), ev("OFFERING_FILED", date(2026, 9, 1)), ev("REVERSE_SPLIT", date(2026, 7, 1))]
    fin = {"warrants": 5e6, "warrant_price": 1.5, "runway_months": 2.2}
    filings = [Filing("DEF 14A", date(2026, 9, 1), "a", ""),
               Filing("10-Q", date(2026, 8, 14), "b", "", report_date="2026-06-30")]
    ms = motives(evs, filings, fin, T, 1.2)
    assert keys(ms) >= {"atm", "offering", "warrants", "cash", "reverse_split", "meeting"}
    w = next(x for x in ms if x.key == "warrants")
    assert "+25%" in w.detail                                     # 행사가 $1.5 vs 현재가 $1.2
    # ATM 종료 뒤엔 ATM 동기 없음, 공모가 실제로 나가면 '공모 대기' 없음
    ms = motives(evs + [ev("ATM_TERMINATED", date(2026, 8, 20), 1), ev("TAKEDOWN", date(2026, 9, 5))], filings, None, T, 1.2)
    assert not keys(ms) & {"atm", "offering"}
    # 다음 10-Q 기간 말일 9/30 → 기한 11/14: 21일 이내일 때만
    ms = motives([], filings, None, date(2026, 11, 1), 1.2)
    p = next(x for x in ms if x.key == "periodic")
    assert p.label == "10-Q D-13"


def test_below_one_streak():
    closes = [(T - timedelta(days=i), 0.8) for i in range(25)] + [(T - timedelta(days=30), 1.1)]
    assert "below_1" in keys(motives([], [], None, T, 0.8, closes))
    assert "below_1" not in keys(motives([], [], None, T, 0.8, closes[:10]))


def bar(et_dt, o, h, lo, c, v):
    return (et_dt.replace(tzinfo=ET).astimezone(timezone.utc).isoformat(), o, h, lo, c, v)


def test_simulate_day_market_buy_and_exits():
    d, nd = "2026-09-14", "2026-09-15"
    bars = [bar(datetime(2026, 9, 14, 21, 0), 1.0, 1.0, 1.0, 1.0, 1000),      # 데이마켓 VWAP 1.0
            bar(datetime(2026, 9, 15, 2, 0), 1.0, 1.0, 1.0, 1.0, 1000),
            bar(datetime(2026, 9, 15, 9, 0), 1.1, 1.2, 1.1, 1.15, 500),        # 프리 마지막 1.15
            bar(datetime(2026, 9, 15, 9, 30), 1.2, 1.3, 1.2, 1.25, 900),       # 정규장 시가 1.2
            bar(datetime(2026, 9, 15, 9, 59), 1.1, 1.1, 0.9, 0.95, 900),       # 개장 30분 0.95
            bar(datetime(2026, 9, 15, 11, 0), 2.0, 2.0, 2.0, 2.0, 900)]        # 구간 밖
    s = simulate(bars, d, nd, close=0.9)
    assert abs(s["day_vwap"] - 1.0) < 1e-9 and abs(s["day_gap"] - (1 / 0.9 - 1) * 100) < 1e-9
    assert abs(s["ret_pre"] - 15) < 1e-6 and abs(s["ret_open"] - 20) < 1e-6 and abs(s["ret_30m"] + 5) < 1e-6
    assert abs(s["ret_best"] - 30) < 1e-6 and abs(s["ret_worst"] + 10) < 1e-6
    assert simulate(bars[2:], d, nd, 0.9)["day_vwap"] is None              # 데이마켓 거래 없음 → 못 산다


def test_find_runners_and_factors(tmp_path):
    rdb = ResearchDB(tmp_path / "r.db")
    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(30)]
    rows = [{"symbol": "PENX", "d": x.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000} for x in days]
    rows[25].update(close=1.5, high=1.5, low=1.0, volume=50000)               # +50% 급등
    rows[26].update(open=1.8, close=1.2, high=1.9)
    rdb.put("daily_bars", rows)
    rs = find_runners(rdb, date(2026, 8, 1))
    assert len(rs) == 1 and rs[0]["d"] == days[25].isoformat() and rs[0]["next_d"] == days[26].isoformat()
    assert abs(rs[0]["change_pct"] - 50) < 1e-9 and rs[0]["close_loc"] == 1.0 and rs[0]["vol_ratio"] == 50
    rs[0].update(day_vwap=1.4, day_gap=-6.7, day_dollar=1e5, ret_pre=10.0, ret_open=12.0, ret_30m=-3.0,
                 short_ratio=0.6, dilution_score=20, motives="atm")
    ft = {(f["factor"], f["group"]): f for f in factor_table(rs)}
    assert ft[("전체", "급등주 전체")]["ret_open"]["mean"] == 12.0
    assert ft[("공매도 비율", "50% 이상")]["n"] == 1 and ft[("공매도 비율", "50% 미만")]["n"] == 0
    assert ft[("부양 동기별", "ATM 진행 중")]["ret_pre"]["hit"] == 1.0
