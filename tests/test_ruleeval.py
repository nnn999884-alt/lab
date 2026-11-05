import sqlite3
from datetime import datetime, timedelta, timezone

from pennybot import ruleeval


def _db(path, symbol="AAA", day="2026-11-03", rise=True):
    """1분봉 40개는 조용 → 41번째 분봉에서 거래 터짐($5만) → 이후 +40% 까지 오름."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE bars_1m (symbol TEXT, ts TEXT, trade_date TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE daily_bars (symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE stock_info (symbol TEXT, name TEXT, shares REAL, list_date TEXT, updated TEXT);""")
    con.execute("INSERT INTO stock_info VALUES (?,?,?,?,?)", (symbol, "x", 3e6, None, None))
    # 일봉 15일: 10일 전 $1 → 어제 $0.5 (−50%), 전날 거래량만 20배
    base = datetime(2026, 10, 15)
    for k in range(15):
        c = 1.0 if k < 5 else 0.5
        v = 2e7 if k == 14 else 1e6
        con.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?)",
                    (symbol, (base + timedelta(days=k)).date().isoformat(), c, c, c, c, v))
    t0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    for i in range(70):
        ts = (t0 + timedelta(minutes=i)).isoformat()
        o = h = lo = c = 0.5
        v = 1000.0
        if i == 20:
            h = 0.6                                     # 30분 변동폭 +20% (윗꼬리)
        if i == 40:
            v = 100000.0                                # 터짐: 이 분 $5만
        if i == 45 and rise:
            h = 0.7                                     # +40%
        con.execute("INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?)", (symbol, ts, day, "PRE", o, h, lo, c, v))
    con.commit()
    con.close()


def test_extract_finds_burst_with_features_and_outcome(tmp_path):
    p = str(tmp_path / "m.db")
    _db(p)
    ev = ruleeval.extract(p, "2026-11-01")
    assert len(ev) == 1
    e = ev[0]
    assert e["ses"] == "PRE" and e["px"] == 0.5 and e["sh"] == 3e6
    assert e["c10"] == -50 and e["pvr"] == 20
    assert e["r30"] >= 15
    assert e["up"] >= 39 and e["ret"] == ruleeval.TP        # +40% 고점 → 익절 규칙에 닿음
    assert ruleeval.score(e) == 5 and ruleeval.rule_session_score(e)


def test_burst_without_future_bars_is_skipped(tmp_path):
    p = str(tmp_path / "m.db")
    _db(p)
    con = sqlite3.connect(p)
    con.execute("DELETE FROM bars_1m WHERE ts > ?", ("2026-11-03T12:42:00+00:00",))   # 터짐 직후 1~2분만 남김
    con.commit()
    con.close()
    assert ruleeval.extract(p, "2026-11-01") == []          # 결과를 못 보는 이벤트는 뺀다 (막 터진 것)


def test_evaluate_splits_selection_and_holdout(tmp_path):
    p = str(tmp_path / "m.db")
    _db(p)
    ev = ruleeval.extract(p, "2026-11-01")
    a = ruleeval.evaluate(ev, "session-score", "2026-11-04")        # 이벤트(11/03)가 기준일 앞 → 선택 기간
    assert a["선택"]["rule"]["n"] == 1 and a["확인"]["rule"]["n"] == 0
    b = ruleeval.evaluate(ev, "session-score", "2026-11-03")        # 기준일 당일부터 확인 기간
    assert b["선택"]["rule"]["n"] == 0 and b["확인"]["rule"]["n"] == 1
    assert b["확인"]["rule"]["hit"] == 100.0 and b["확인"]["rule"]["net"] == ruleeval.TP - ruleeval.COST
    assert "확인 기간에 규칙에 해당한 이벤트가 없어서" in ruleeval.text(a, "session-score", "2026-11-04")
    assert "표본 부족" in ruleeval.text(b, "session-score", "2026-11-03")        # 1건으로는 판정하지 않는다


def test_session_thresholds_differ_by_session():
    e = {"px": 0.5, "sh": 3e6, "c10": -30, "pvr": 1, "ptu": 1, "r30": 3}      # 조건 3개 충족 (가격·발행주식·10일 하락)
    assert ruleeval.score(e) == 3
    assert ruleeval.rule_session_score({**e, "ses": "PRE"}) is True            # 프리는 3점
    assert ruleeval.rule_session_score({**e, "ses": "REGULAR"}) is False       # 정규장은 4점
    assert ruleeval.rule_session_score({**e, "ses": "AFTER"}) is False
    four = {**e, "r30": 20}
    assert ruleeval.rule_session_score({**four, "ses": "REGULAR"}) is True
    assert ruleeval.rule_session_score({**four, "ses": "AFTER"}) is True
    assert ruleeval.rule_session_score({**four, "ses": "DAY"}) is False        # 데이마켓은 알림 없음


def test_wilson_interval_is_wide_for_small_samples():
    lo, hi = ruleeval.wilson(1, 1)                  # 1건 중 1건 적중 = 100% 가 아니라 '모른다'에 가깝다
    assert lo < 25 and hi > 99
    lo2, hi2 = ruleeval.wilson(100, 1000)           # 10%, 표본 크면 좁다 (약 8.3~12.0%)
    assert 8 < lo2 < 10 < hi2 < 12.5
    assert ruleeval.wilson(0, 0) == (0.0, 0.0)
