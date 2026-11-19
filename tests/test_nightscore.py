from datetime import date, timedelta

from pennybot import nightscore as N


def _d(i, n=60):
    return (date(2026, 11, 4) - timedelta(days=n - 1 - i)).isoformat()


def _quiet(n=60, px=2.0):
    return [(f"2026-08-{i:02d}", px, px * 1.01, px * 0.99, px, 10000) for i in range(n)]


def test_quiet_box_scores_negative():
    r = N.score(_quiet())
    assert r["score"] < 0 and "5일 박스" in r["minus"] and "변동 작음" in r["minus"]


def test_beaten_down_active_runner_scores_high():
    rows = []
    px = 3.0
    for i in range(50):                                 # 크게 출렁이며 빠짐 (하루 폭 20%↑)
        px *= 0.96
        rows.append((f"d{i}", px * 1.1, px * 1.15, px * 0.9, px, 50000))
    rows[-12] = ("x", rows[-13][4], rows[-13][4] * 1.7, rows[-13][4], rows[-13][4] * 1.05, 500000)   # 20일 안 +50% 급등, 거래 터짐
    p = rows[-1][4]
    rows.append(("y", p, p * 1.02, p * 0.85, p * 0.88, 400000))                # 전날 −12% · 거래 많음
    r = N.score(rows)
    for k in ("20일 하루 변동 15%↑", "60일 고점 대비 −70%↓", "전날 −10%↓", "20일 안 +50% 급등", "전날 거래 평소 3배↑"):
        assert k in r["why"], k
    assert r["score"] >= N.MIN_SCORE and N.p2x(r["score"]) > 0


def test_price_range_and_short_history():
    assert N.score(_quiet(px=20.0)) is None
    assert N.score(_quiet(n=30)) is None


def test_candidates_query_filters_and_sorts():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE daily_bars (symbol, d, open, high, low, close, volume)")
    for s, px in (("Q", 2.0), ("Z", 0.6)):
        for i in range(60):
            d = _d(i)
            v = 1e6 if (s == "Z" and i == 59) else 1e4
            p = px * (0.97 ** i) if s == "Z" else px
            hi = p * (1.25 if s == "Z" else 1.01)
            con.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?)", (s, d, p, hi, p * (0.85 if s == "Z" else 0.99), p, v))
    q = lambda sql, a=(): con.execute(sql, a).fetchall()
    out = N.candidates(q, "2026-11-05", min_score=3)
    assert [r["symbol"] for r in out] == ["Z"]
    assert N.candidates(q, "2026-11-05", symbols=["Q"], min_score=-9)[0]["symbol"] == "Q"


def test_collector_night_cands_job_sets_priority_and_sends(tmp_path):
    from datetime import datetime, timezone
    from pennybot.collector.runner import Collector
    from pennybot.collector.store import Store
    from pennybot.config import CollectorConfig
    db = str(tmp_path / "m.db"); st = Store(db)
    with st.db:
        for i in range(60):
            d = _d(i)
            p = 0.6 * (0.97 ** i)
            st.db.execute("INSERT INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('Z', ?, ?, ?, ?, ?, ?)",
                          (d, p, p * 1.25, p * 0.85, p, 1e6 if i == 59 else 1e4))
    sent = []

    class Nt:
        def send(self, text, **k): sent.append(text)
    col = Collector(None, st, None, CollectorConfig(db_path=db), notifier=Nt())
    col.universe = {"Z": {"name": "Zed Corp"}}
    col.clock = lambda: datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    import pennybot.nightscore as NS
    old = NS.MIN_SCORE
    try:
        out = col._night_cands_job("2026-11-05")
    finally:
        NS.MIN_SCORE = old
    assert col._ncand_d == "2026-11-05"
    if True:                                            # 점수 5점↑ → 감시 앞쪽 + 알림
        assert col._ncands == ["Z"] and "🔭 오늘 급등 후보" in sent[0] and "Z" in sent[0]
    assert col.night_cands(datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)) is False   # 같은 거래일엔 다시 안 함


def test_series_and_rank_with_model():
    import sqlite3
    import numpy as np
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE daily_bars (symbol, d, open, high, low, close, volume)")
    for s, px in (("A", 1.0), ("B", 2.0)):
        for i in range(70):
            d = _d(i, 70)
            con.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?)", (s, d, px, px * 1.1, px * 0.9, px, 1e5))
    q = lambda sql, a=(): con.execute(sql, a).fetchall()
    rows = [tuple(r[1:]) for r in q("SELECT * FROM daily_bars WHERE symbol='A' ORDER BY d")]
    days, X, y, pc = N.series(rows)
    assert X.shape == (10, len(N.FEATS)) and days[-1] is None and y[-1] is None and y[0] is False

    class M:                                            # B 가 더 높게 나오는 가짜 모델
        def predict_proba(self, X):
            p = (X[:, N.FEATS.index("lpx")] > 0.5).astype(float) * 0.9 + 0.05
            return np.column_stack([1 - p, p])
    out = N.rank(q, "2026-11-05", model={"model": M()})
    assert [r["symbol"] for r in out] == ["B", "A"] and out[0]["p"] > out[1]["p"]
    assert [r["symbol"] for r in N.rank(q, "2026-11-05")][0] in ("A", "B")      # 모델 없으면 모양 점수 순


def test_rotation_gives_candidates_their_share(tmp_path):
    from datetime import datetime, timezone
    from pennybot.collector.runner import Collector
    from pennybot.collector.store import Store
    from pennybot.config import CollectorConfig
    st = Store(str(tmp_path / "m.db"))
    col = Collector(None, st, None, CollectorConfig(db_path=str(tmp_path / "m.db")), notifier=None)
    col.universe = {f"S{i:03d}": {"name": ""} for i in range(300)}
    col._ncands = [f"S{i:03d}" for i in range(250, 300)]
    col._lowpri = {"S299"}
    col.select_watch(datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc), None)
    rot = [s for s in col.watch]
    got = sum(1 for s in rot if s in set(col._ncands))
    assert got == min(col.NCAND_ROT, col._watch_buckets["rotation"]) and "S299" in col._rotation_pool([])


def test_series_all_labels_two_thresholds_without_future_leak():
    rows = [(f"d{i:03d}", 1.0, 1.02, 0.98, 1.0, 1e6) for i in range(70)]
    rows[65] = ("d065", 1.0, 1.4, 0.98, 1.3, 1e6)                    # 그날 고가 +40% (2배 아님)
    days, X, y, pc = N.series_all(rows)
    k = days.index("d065")
    assert y[1.3][k] is True and y[2.0][k] is False and y[1.3][-1] is None
    cut = N.series_all(rows[:65])                                     # 65번째 줄 이후를 잘라도 앞쪽 특징은 같아야 함
    assert (cut[1][:len(cut[0]) - 1] == X[:len(cut[0]) - 1]).all()


def test_train_picks_mode_logs_and_rank_uses_it(tmp_path):
    import json
    import sqlite3
    import random
    random.seed(1)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE daily_bars (symbol, d, open, high, low, close, volume)")
    start = date(2025, 6, 1)
    for s in range(60):
        px = 0.5 + s * 0.1
        for i in range(330):
            d = (start + timedelta(days=i)).isoformat()
            r = random.random()
            hi = px * (2.1 if r < 0.015 else 1.35 if r < 0.05 else 1.05)
            con.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?)", (f"S{s}", d, px, hi, px * 0.95, px, 2e6))
    q = lambda sql, a=(): con.execute(sql, a).fetchall()
    m = N.train(q, str(tmp_path / "m.db"), (start + timedelta(days=340)).isoformat())
    assert m and m["mode"] in "ABC" and set(m["compare"]) == {"A", "B", "C"} and m["fresh"]
    assert "🧠 오늘 학습" in N.learn_text(m)
    assert N.load_model(str(tmp_path / "m.db"))["fresh"] is False and N.learn_text(N.load_model(str(tmp_path / "m.db"))) == ""
    log = (tmp_path / "learn_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["mode"] == m["mode"]
    out = N.rank(q, (start + timedelta(days=340)).isoformat(), model=m)
    assert out and all(r["p"] is not None for r in out)
