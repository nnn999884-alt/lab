import random
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from pennybot import rankmodel, briefing


def _db(tmp_path, n_sym=60, n_days=200, seed=1):
    """가짜 일봉: 거래량이 터진 다음 날 고가 +40% 가 자주 나오게."""
    rnd = random.Random(seed)
    p = tmp_path / "market.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE daily_bars (symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, d))")
    con.execute("CREATE TABLE stock_info (symbol TEXT PRIMARY KEY, name TEXT, shares REAL)")
    d0 = date(2026, 1, 1)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(n_days)]
    for s in range(n_sym):
        sym = f"S{s:03d}"
        con.execute("INSERT INTO stock_info VALUES (?,?,?)", (sym, sym, 5e6))
        c, boom = 1.0, False
        for d in days:
            o = c
            hi = c * (1.45 if boom else 1.03)
            lo = c * 0.97
            c = c * (1.0 + rnd.uniform(-0.03, 0.03))
            boom = rnd.random() < 0.04
            v = 2e6 if boom else 1e5
            con.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?)", (sym, d, o, max(hi, c), min(lo, c), c, v))
    con.commit()
    con.close()
    return str(p), days


def test_feats_needs_62_bars_and_filters():
    bars = [(f"d{i:03d}", 1, 1.05, 0.95, 1.0, 1e5) for i in range(70)]
    f = rankmodel.feats(bars, 5e6)
    assert f and set(rankmodel.FEATURES) <= set(f)
    assert rankmodel.feats(bars[:61], 5e6) is None
    assert rankmodel.feats([b[:4] + (30.0, 1e5) for b in bars], 5e6) is None      # 가격 범위 밖
    assert rankmodel.feats([b[:5] + (100,) for b in bars], 5e6) is None          # 거래대금 부족
    assert len(rankmodel.vec(f)) == len(rankmodel.FEATURES)


def test_top_hit():
    probs = [0.9, 0.1, 0.8, 0.7]
    y = [True, True, False, True]
    days = ["a", "a", "b", "b"]
    assert rankmodel.top_hit(probs, y, days, top=1) == (1, 2)   # a: 0.9 맞음, b: 0.8 틀림
    assert rankmodel.top_hit(probs, y, days, top=2) == (3, 4)


def test_train_score_and_briefing(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    db, days = _db(tmp_path)
    monkeypatch.setattr(rankmodel, "TRAIN_DAYS", 400)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert rankmodel.needs_train(db, now)
    m = rankmodel.train(db, now)
    assert m and m["check"] is None and rankmodel.model_path(db).exists()
    assert not rankmodel.needs_train(db, now + timedelta(days=5))
    assert rankmodel.needs_train(db, now + timedelta(days=31))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    mq = lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()]
    sc = rankmodel.score_today(mq, db, days[-1])
    assert sc and all(0 <= r["prob"] <= 100 for r in sc)
    assert sc == sorted(sc, key=lambda r: -r["prob"])
    # 거래 터진 종목이 위로
    boom = {r[0] for r in con.execute("SELECT symbol FROM daily_bars WHERE d=? AND volume>1e6", (days[-1],))}
    if boom:
        assert sc[0]["symbol"] in boom
    con.close()


def test_retrain_records_check(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    db, days = _db(tmp_path, n_days=220)
    monkeypatch.setattr(rankmodel, "TRAIN_DAYS", 400)
    # 첫 학습: 앞쪽 데이터만 보이게 through 를 앞으로 당긴다
    m = rankmodel.train(db, datetime(2026, 8, 1, tzinfo=timezone.utc))
    m["through"] = days[150]
    import pickle
    rankmodel.model_path(db).write_bytes(pickle.dumps(m))
    m2 = rankmodel.train(db, datetime(2026, 9, 1, tzinfo=timezone.utc))
    c = m2["check"]
    assert c and c["from"] > days[150] and c["n"] > 0 and len(m2["history"]) == 1
    assert "지난 모델 실제 적중" in rankmodel.check_text(m2)


def test_briefing_text_shows_prob():
    b = {"date": "2026-11-09", "model": {"through": "2026-11-06", "check": "지난 모델 실제 적중 21%"},
         "picks": [{"symbol": "ABC", "prob": 23.4, "tier": 2, "close": 1.23, "vol_ratio": 12, "labels": []}]}
    t = briefing.text(b)
    assert "확률 순" in t and "ABC 23% ★★ $1.23" in t and "실제 적중 21%" in t
