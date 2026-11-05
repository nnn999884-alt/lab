import sqlite3
from datetime import datetime, timedelta, timezone

from pennybot import haltresume
from pennybot.precursor import alert_grade, live, momo_flags


def _bars(n, start, price=1.0, vol=100.0, last_vol=None, jump=None):
    out = []
    for i in range(n):
        t = start + timedelta(minutes=i)
        p = price * (jump if jump and i >= n - 3 else 1.0)
        v = last_vol if last_vol is not None and i >= n - 5 else vol
        out.append((t.isoformat(), p, p * 1.01, p * 0.99, p, v))
    return out


def test_live_range30_and_vol5():
    t0 = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
    b = _bars(40, t0, last_vol=1000.0, jump=1.3)
    m = live(b, 1.0, datetime.fromisoformat(b[-1][0]))["metrics"]
    assert m["range30"] > 25
    assert 9 < m["vol5_x"] < 11
    assert momo_flags(m, "PRE") == [f"30분 변동폭 {m['range30']:.0f}%", f"5분 거래량 {m['vol5_x']:.1f}배"]
    assert momo_flags(m, "DAY") == [f"30분 변동폭 {m['range30']:.0f}%"]       # 5분 거래량 배수는 프리·정규장만


def test_quiet_bars_no_flags_and_grade_upgrade():
    t0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    b = _bars(40, t0)
    m = live(b, 1.0, datetime.fromisoformat(b[-1][0]))["metrics"]
    assert momo_flags(m, "REGULAR") == []
    ts = "2026-11-04T15:40:00+00:00"                    # 동부 10:40 (프리 창 밖)
    assert alert_grade(ts, 0, None, None, 0.1, 5e7, 1e3)[0] == "C"
    g, why = alert_grade(ts, 0, None, None, 0.1, 5e7, 1e3, ["30분 변동폭 25%"])
    assert g == "B" and "30분 변동폭 25%" in why


def _db(tmp_path):
    p = tmp_path / "m.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE halts (symbol, halt_ts, reason, reason_ko, resume_ts)")
    con.execute("CREATE TABLE bars_1m (symbol, ts, trade_date, session, open, high, low, close, volume)")
    return p, con


def test_halt_resume_first_vs_late(tmp_path):
    p, con = _db(tmp_path)
    h = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    r = h + timedelta(minutes=5)
    con.execute("INSERT INTO halts VALUES ('ABC', ?, 'LUDP', '변동성 정지(LULD)', ?)", (h.isoformat(), r.isoformat()))
    iso = haltresume._iso
    con.execute("INSERT INTO bars_1m VALUES ('ABC', ?, '2026-11-03', 'REGULAR', 2, 2, 2, 2.0, 100)", (iso(h - timedelta(minutes=10)),))
    con.execute("INSERT INTO bars_1m VALUES ('ABC', ?, '2026-11-03', 'REGULAR', 1.6, 1.6, 1.6, 1.6, 100)", (iso(h),))
    # 재개 첫 봉 1.0 → 다음 봉부터 1.25 (첫 가격이면 +20% 익절, 1분 뒤 1.25 에 사면 결과는 보유 끝 종가)
    for i in range(130):
        o = 1.0 if i == 0 else 1.25
        con.execute("INSERT INTO bars_1m VALUES ('ABC', ?, '2026-11-03', 'REGULAR', ?, ?, ?, ?, 100)",
                    (iso(r + timedelta(minutes=i + 1)), o, o * (1.2 if i == 0 else 1.0), o, o))
    con.commit()
    evs = haltresume.events(str(p))
    assert len(evs) == 1 and evs[0]["drop"] < -10
    assert evs[0]["first"] == haltresume.TP - haltresume.COST
    assert abs(evs[0]["late1"] - (-haltresume.COST)) < 1e-9
    out = haltresume.text(evs)
    assert "표본 부족" in out and "ABC" in out


def test_halt_resume_skips_up_halts(tmp_path):
    p, con = _db(tmp_path)
    h = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    con.execute("INSERT INTO halts VALUES ('UP', ?, 'LUDP', 'x', ?)", (h.isoformat(), (h + timedelta(minutes=5)).isoformat()))
    con.execute("INSERT INTO bars_1m VALUES ('UP', ?, 'd', 'REGULAR', 1, 1, 1, 1.0, 1)", (haltresume._iso(h - timedelta(minutes=10)),))
    con.execute("INSERT INTO bars_1m VALUES ('UP', ?, 'd', 'REGULAR', 2, 2, 2, 2.0, 1)", (haltresume._iso(h),))
    con.commit()
    assert haltresume.events(str(p)) == []
    assert "해당 정지 없음" in haltresume.text([])


def test_alerted_ignores_previous_after_hours():
    from pennybot.collector.runner import Collector

    class S:
        def query(self, q, args):
            return [("2026-11-04T22:44:05+00:00",)]
    f = Collector._alerted
    assert f(S(), "2026-11-04", "ZNB") == "2026-11-04T22:44:05+00:00"
    assert f(S(), "2026-11-04", "ZNB", "2026-11-05T01:00:00+00:00") is None     # 어제 애프터장 알림 → 오늘은 새로
    assert f(S(), "2026-11-04", "ZNB", "2026-11-04T01:00:00+00:00") == "2026-11-04T22:44:05+00:00"
