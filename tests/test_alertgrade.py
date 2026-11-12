from pennybot import alertgrade as G


def test_waves_counts_up_legs_and_prior_highs():
    n, highs = G.waves([1.0, 1.2, 1.05, 1.2, 1.3, 1.1, 1.25, 1.4])   # 1.3 → 1.1(−15%) → 다시
    assert n == 3 and highs == [1.2, 1.3]
    assert G.waves([1.0, 1.05, 1.02])[0] == 1


def test_grade_warns_quiet_low_move_and_rewards_history_and_breakout():
    quiet = {"pc": 1.0, "run5": 0, "prev_vx": 1.0, "prev": 2.0}
    hot = {"pc": 1.0, "run5": 80, "prev_vx": 4.0, "prev": -20.0}
    w = G.grade(quiet, 1.05, 1, False)
    assert w["skip"] and w["key"] == "⚠"
    assert not G.grade(quiet, 1.15, 1, False)["skip"]                       # 이미 +15% 면 ⚠ 아님
    b = G.grade(hot, 1.3, 3, True)
    assert b["key"] == "돌파" and "오늘 3번째 파동 · 직전 고점 돌파" in b["line"] and "★★" in b["line"]
    assert G.grade(hot, 1.3, 1, False)["key"] == "★★1"
    assert G.grade(None, 1.0, 1, False) == {"skip": False, "key": None, "line": ""}


def test_day_feats():
    daily = [(f"d{i}", 1.0, 1.0, 1000) for i in range(20)] + [("p2", 1.0, 1.0, 1000), ("p1", 1.6, 1.5, 5000)]
    f = G.day_feats(daily)
    assert f["pc"] == 1.5 and round(f["prev"]) == 50 and f["run5"] >= 59 and f["prev_vx"] > 3


def test_gsend_skips_warn_and_appends_line(tmp_path):
    from datetime import datetime, timezone
    from pennybot.collector.runner import Collector
    from pennybot.collector.store import Store
    from pennybot.config import CollectorConfig
    db = str(tmp_path / "m.db"); st = Store(db)
    with st.db:
        for i in range(25):
            st.db.execute("INSERT INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('Q', ?, 1, 1, 1, 1, 1000)", (f"2026-10-{i + 1:02d}",))
            st.db.execute("INSERT INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('H', ?, 1, ?, 1, 1, ?)",
                          (f"2026-10-{i + 1:02d}", 1.9 if i == 23 else 1, 9000 if i == 24 else 1000))
    sent = []

    class N:
        def send(self, text, **k): sent.append(text)
    col = Collector(None, st, None, CollectorConfig(db_path=db), notifier=N())
    col.clock = lambda: datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)
    assert col._gsend("Q", 1.05, "🔁 재가속 · Q") is False and not sent      # ⚠: 조용한 종목 +5%
    assert col._gsend("H", 1.05, "🔁 재가속 · H") is True
    assert "★★" in sent[0] and "비슷한 알림" in sent[0]
