import sqlite3

from pennybot import overnight


def _db(tmp_path):
    p = tmp_path / "m.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE daily_bars(symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)")
    c.execute("CREATE TABLE bars_1m(symbol TEXT, ts TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, trade_date TEXT)")
    for i in range(25):   # 평범한 날 25일 (거래량 1만)
        c.execute("INSERT INTO daily_bars VALUES('ABC',?,1,1.02,0.98,1.0,10000)", (f"2026-10-{i + 1:02d}" if i < 31 else "x",))
    c.execute("INSERT INTO daily_bars VALUES('ABC','2026-10-26',1,1.04,0.97,1.01,500000)")   # 50배, 등락 +1%, 변동폭 7%
    c.execute("INSERT INTO daily_bars VALUES('ABC','2026-10-27',1.2,1.3,1.1,1.2,10000)")
    # 다음 거래일 분봉: 데이마켓 1.02 에 거래, 정규장 시가 1.20
    c.execute("INSERT INTO bars_1m VALUES('ABC','2026-10-27T00:10:00+00:00','DAY',1.02,1.02,1.02,1.02,1000,'2026-10-27')")
    c.execute("INSERT INTO bars_1m VALUES('ABC','2026-10-27T13:30:00+00:00','REGULAR',1.20,1.25,1.19,1.22,5000,'2026-10-27')")
    c.commit()
    c.close()
    return str(p)


def test_qualifies_boundaries():
    assert overnight.qualifies(20, 0, 10, 1.0, 3e5)
    assert not overnight.qualifies(19.9, 0, 10, 1.0, 3e5)      # 거래량 부족
    assert not overnight.qualifies(30, 6, 10, 1.0, 3e5)        # 가격이 움직임
    assert not overnight.qualifies(30, 0, 20, 1.0, 3e5)        # 변동폭 큼
    assert not overnight.qualifies(30, 0, 10, 3.5, 3e5)        # $3 초과
    assert not overnight.qualifies(30, 0, 10, 1.0, 1e5)        # 거래대금 부족


def test_find_and_outcome_use_daymarket_price(tmp_path):
    res = overnight.evaluate(_db(tmp_path), "2026-10-26")
    assert [e["d"] for e in res["events"]] == ["2026-10-26"]
    o = res["events"][0]["out"]
    assert abs(o["ret"] - ((1.20 / 1.02 - 1) * 100 - overnight.COST)) < 1e-6      # 데이마켓 가격 기준
    assert abs(o["ret_close"] - ((1.20 / 1.01 - 1) * 100 - overnight.COST)) < 1e-6  # 종가 기준(참고)
    assert "표본 부족" in overnight.text(res)           # 1건뿐이면 결론 안 냄


def test_no_next_day_bars_means_pending(tmp_path):
    p = _db(tmp_path)
    c = sqlite3.connect(p); c.execute("DELETE FROM bars_1m"); c.commit(); c.close()
    res = overnight.evaluate(p, "2026-10-26")
    assert res["events"] and not res["done"]
    assert "분봉 없음" in overnight.text(res)
