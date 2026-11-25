"""gaps 순수 함수 — 손으로 계산한 작은 입력 (난수·가짜 시장 데이터 없음)."""
from pennybot2.research import gaps


def test_market_days_ignores_none_and_partial_days():
    assert gaps.market_days({"2026-11-17": 500, "2026-11-16": 300, "2026-11-18": 299, None: 9999}) == ["2026-11-16", "2026-11-17"]


def test_missing_days():
    have = {"A": {"d1", "d3"}, "B": {"d1", "d2", "d3"}, "C": {"d1"}}
    assert gaps.missing_days(have, ["d1", "d2", "d3"]) == {"A": ["d2"]}      # B 는 빠진 날 없음, C 는 마지막 기록(d1) 뒤라 센 안 함


def test_build_jobs_order_filters_and_done():
    days = ["d1", "d2", "d3", "d4"]
    have = {"RUN": {"d1": 10, "d4": 10}, "BIG": {"d1": 400, "d3": 400}, "SMALL": {"d1": 50, "d4": 50}, "FULL": {"d1": 1, "d2": 1, "d3": 1, "d4": 1}}
    jobs = gaps.build_jobs(have, days, {"RUN"}, done={("RUN", "d3")})
    # 급등 이력 RUN 먼저(최근 날부터, d3 는 처리됨 → d2), 그다음 BIG(d2), SMALL(중앙값 50<100)·FULL(빠진 날 없음) 제외
    assert jobs == [("RUN", "d2"), ("BIG", "d2")]


def test_parse_rows_end_time_session_and_date_filter():
    rows = [
        {"date": "20261123", "loctime": "100000", "open": "1.0", "high": "1.2", "low": "0.9", "close": "1.1", "exevol": "500"},   # 월요일 10:00 ET 시작 → 끝 10:01 ET = 15:01Z, REGULAR
        {"date": "20261122", "loctime": "210000", "open": "2", "high": "2", "low": "2", "close": "2", "exevol": "7"},               # 일요일 밤 21:00 ET → 11/23 영업일의 데이마켓
        {"date": "20261120", "loctime": "100000", "open": "3", "high": "3", "low": "3", "close": "3", "exevol": "9"},               # 다른 영업일(11/20) → 버림
        {"date": "20261123", "loctime": "100100", "open": "x", "high": "1", "low": "1", "close": "1", "exevol": "1"},               # 값 깨짐 → 버림
    ]
    out = gaps.parse_rows(rows, "2026-11-23")
    assert [(r[1], r[2], r[3], r[4], r[8]) for r in out] == [
        ("2026-11-23T15:01:00+00:00", "2026-11-23", "REGULAR", 1.0, 500.0),
        ("2026-11-23T02:01:00+00:00", "2026-11-23", "DAY", 2.0, 7.0),
    ]


def test_exch_order_master_market_first_then_rest():
    assert gaps.exch_order("NYSE") == ["81", "82", "83"]
    assert gaps.exch_order("NASDAQ") == ["82", "81", "83"]
    assert gaps.exch_order("") == ["82", "81", "83"]          # 마스터에 없는 종목(AMEX 일 수 있음)은 전부 시도
