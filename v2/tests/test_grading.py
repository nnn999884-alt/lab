import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pennybot2.core.store import Store
from pennybot2.grading import outcomes as oc
from pennybot2.grading import run as grun
from pennybot2.grading import stats

T = 1_700_000_000.0


def bars(prices, start=T, step=60):
    """prices: [(o,h,l,c)] 또는 숫자(= 한 가격)"""
    out = []
    for i, p in enumerate(prices):
        o, h, l, c = (p, p, p, p) if isinstance(p, (int, float)) else p
        out.append(oc.Bar(start + i * step, o, h, l, c))
    return out


def test_entry_is_next_bar_open_not_alert_minute():
    b = bars([1.0, 1.0, 2.0, 2.0])               # 알림은 두 번째 봉 시작 직후 → 진입은 세 번째 봉 시가 2.0
    o = oc.outcome(b, T + 61, cost_pct=0)
    assert o["entry"] == 2.0 and o["entry_delay_s"] == pytest.approx(59)


def test_race_win_loss_same_bar_is_loss_and_none():
    e = 1.0
    assert oc.race(bars([(1, 1.11, 1, 1.1)]), e, 0.10, 0.05) == "win"
    assert oc.race(bars([(1, 1, 0.94, 0.95)]), e, 0.10, 0.05) == "loss"
    assert oc.race(bars([(1, 1.2, 0.9, 1.0)]), e, 0.10, 0.05) == "loss"        # 한 봉에서 둘 다 → 손실
    assert oc.race(bars([1.0, 1.02, 0.99]), e, 0.10, 0.05) == "none"
    assert oc.race(bars([(1, 1.06, 1, 1.05), (1, 1.12, 1, 1.1)]), e, 0.10, 0.05) == "win"


def test_cost_reduces_returns_and_windows_are_equal_length():
    prices = [1.0] * 400
    o = oc.outcome(bars(prices), T - 1, cost_pct=1.0)
    assert o["ret30"] == pytest.approx(-2.0) and o["ret120"] == pytest.approx(-2.0)      # 왕복 비용 2%
    assert o["mfe360"] == 0 and o["hit20_360"] is False


def test_missing_data_is_unknown_not_filled():
    o = oc.outcome(bars([1.0] * 10), T - 1)           # 10분치뿐 → 30분·2시간·6시간 창은 미확인
    assert o["ret30"] is None and o["ret120"] is None and o["mfe360"] is None and o["hit20_360"] is False
    assert oc.outcome(bars([1.0, 1.0]), T + 500) is None                      # 진입할 봉 없음


def test_wilson_interval_sane():
    lo, hi = stats.wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert stats.wilson(0, 0) == (0.0, 1.0)
    lo, hi = stats.wilson(1, 3)
    assert hi - lo > 0.5                                  # 표본 3건이면 구간이 매우 넓다


def test_split_periods_is_chronological_and_even():
    rows = [{"ts": f"2026-01-{d:02d}"} for d in (9, 1, 5, 3, 7, 2, 8, 4, 6)]
    p = stats.split_periods(rows)
    assert [len(x) for x in p] == [3, 3, 3] and max(r["ts"] for r in p[0]) < min(r["ts"] for r in p[1])


def test_verdict_needs_enough_samples():
    g = [{"ts": f"2026-01-{i:02d}", "r10_5": "win"} for i in range(1, 10)]
    c = [{"ts": "x", "r10_5": "loss"}] * 50
    assert stats.verdict(g, c)[0].startswith("표본 부족")


# ---------- 모의 데이터로 끝까지: 효과 있는 등급 vs 효과 없는 등급 ----------
def build_db(path, seed=7):
    rnd = random.Random(seed)
    s = Store(path)
    s.db.execute("CREATE TABLE IF NOT EXISTS events (ts TEXT, symbol TEXT, kind TEXT, text TEXT, value REAL, PRIMARY KEY (ts, symbol, kind))")   # 기존 pennybot DB 의 알림 표 흉내
    start = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)          # 정규장 시작
    n = 0
    for grade, edge in (("★★", True), ("★", False), ("등급 없음", False)):
        for i in range(150):
            sym = f"{ {'★★': 'AA', '★': 'BB', '등급 없음': 'CC'}[grade] }{i}"
            day = start + timedelta(days=i % 30)
            if day.weekday() >= 5:
                day += timedelta(days=2)
            p, rows = 1.0, []
            boost = edge and rnd.random() < 0.55
            for m in range(400):
                drift = 0.012 if (boost and 31 <= m < 45) else 0.0          # 알림(30분) 직후 15분 급등
                p = max(0.05, p * (1 + drift + rnd.gauss(0, 0.004)))
                t = (day + timedelta(minutes=m)).isoformat()
                rows.append((sym, t, "d", "REGULAR", p, p * 1.002, p * 0.998, p, 1000))
            s.put_bars(rows)
            alert_ts = (day + timedelta(minutes=30, seconds=10)).isoformat()
            s.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                         (alert_ts, sym, "burst", f"{sym} 알림\n{grade}", 0.0))
            n += 1
    s.db.commit()
    return n


def test_grade_of_text():
    assert grun.grade_of("x\n★★ (전날 거래 5배)") == "★★"
    assert grun.grade_of("A등급 …") == "A등급"
    assert grun.grade_of("등급 없음") == "등급 없음" and grun.grade_of("아무것도") == "-"
    assert grun.grade_of("★ABC 급등 ★ 순위 1위\n등급 없음") == "등급 없음"      # 본문 중간의 별은 등급이 아님
    assert grun.grade_of("ABC\n★ (전날 +12%)") == "★" and grun.grade_of("ABC\n★★★") == "★★★"


def test_end_to_end_distinguishes_edge_from_noise(tmp_path, capsys):
    db_path = tmp_path / "old.db"
    build_db(db_path)
    assert grun.main([str(db_path), "--controls=400", "--cost=0.5"]) == 0
    out = capsys.readouterr().out
    section = out.split("== 등급별 ==")[1].split("== 알림 종류별 ==")[0]
    star2 = next(l for l in section.splitlines() if l.strip().startswith("★★") and "판정" not in l and "|" in l)
    star1 = next(l for l in section.splitlines() if l.strip().startswith("★ ") and "|" in l)
    none_ = next(l for l in section.splitlines() if l.strip().startswith("등급 없음") and "|" in l)
    assert "대조군보다 낫고 세 기간 모두 유지" in star2          # 진짜 효과가 있는 등급은 통과
    assert "대조군보다 낫고 세 기간 모두 유지" not in star1       # 효과 없는 등급은 통과하면 안 됨
    assert "대조군보다 낫고 세 기간 모두 유지" not in none_
    assert "채점 가능 450건" in out


def test_reads_v2_signals_table_too(tmp_path):
    s = Store(tmp_path / "v2.db")
    s.add_signal("2026-11-02T15:00:10+00:00", "ABC", "surge", 1.0, 12.0, 6, "t\n★★")
    db = sqlite3.connect(tmp_path / "v2.db")
    a = grun.load_alerts(db)
    assert len(a) == 1 and a[0]["kind"] == "surge"
