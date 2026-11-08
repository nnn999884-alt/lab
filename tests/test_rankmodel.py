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
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)   # 가짜 일봉 끝(07-19) 뒤
    assert rankmodel.needs_train(db, now)
    m = rankmodel.train(db, now)
    assert m and m["check"] is None and rankmodel.model_path(db).exists()
    assert not rankmodel.needs_train(db, now + timedelta(days=5))       # 새 일봉 없음
    assert rankmodel.needs_train(db, now + timedelta(days=31))          # 안전장치: 30일
    con = sqlite3.connect(db)
    last = con.execute("SELECT MAX(d) FROM daily_bars").fetchone()[0]
    con.execute("INSERT INTO daily_bars SELECT symbol, date(?, '+1 day'), open, high, low, close, volume FROM daily_bars WHERE d=?", (last, last))
    con.commit()
    con.close()
    assert rankmodel.needs_train(db, now + timedelta(days=5))           # 새 거래일 들어옴
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
    assert "실제 적중" in rankmodel.check_text(m2)


def test_briefing_text_shows_prob():
    b = {"date": "2026-11-09", "model": {"through": "2026-11-06", "check": "지난 모델 실제 적중 21%"},
         "picks": [{"symbol": "ABC", "prob": 23.4, "tier": 2, "close": 1.23, "vol_ratio": 12, "labels": []}]}
    t = briefing.text(b)
    assert "확률 순" in t and "ABC 23% ★★ $1.23" in t and "실제 적중 21%" in t


def test_recent_score_sums_last_days():
    h = [{"from": f"2026-10-{d:02d}", "to": f"2026-10-{d:02d}", "days": 1, "hit": 2, "n": 12, "rule_hit": 1, "rule_n": 12} for d in range(1, 31)]
    c = rankmodel.recent_score({"history": h}, days=20)
    assert c["days"] == 20 and c["n"] == 240 and c["hit"] == 40 and c["from"] == "2026-10-11"
    assert "최근 20거래일 실제 적중 17%" in rankmodel.check_text({"history": h}) and "예전 ★ 규칙 8%" in rankmodel.check_text({"history": h})
    assert rankmodel.check_text({}) == ""


def test_probs_cached_and_empty_without_model(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    db, days = _db(tmp_path)
    assert rankmodel.probs(db, days[-1]) == {}                       # 모델 없음
    monkeypatch.setattr(rankmodel, "TRAIN_DAYS", 400)
    rankmodel.train(db, datetime(2026, 7, 25, tzinfo=timezone.utc))
    p1 = rankmodel.probs(db, days[-1])
    assert p1 and all(0 <= v <= 100 for v in p1.values())
    monkeypatch.setattr(rankmodel, "score_today", lambda *a: (_ for _ in ()).throw(AssertionError("다시 계산함")))
    assert rankmodel.probs(db, days[-1]) is p1                       # 캐시


# ---------------- 새벽 모델

def test_night_window_and_feats():
    a0, a1, b1 = rankmodel.night_window("2026-11-05", "2026-11-06")
    assert a0 == "2026-11-05T21:00:00+00:00" and a1 == "2026-11-06T01:00:00+00:00" and b1 == "2026-11-06T08:45:00+00:00"
    bars = [("2026-11-05T22:00:00+00:00", 1, 1.2, 1, 1.1, 1000), ("2026-11-06T02:00:00+00:00", 1.1, 1.5, 1.1, 1.4, 5000)]
    f, last = rankmodel.night_feats(bars, 1.0, 1e6, a1)
    assert len(f) == len(rankmodel.NIGHT_FEATURES) and last == 1.4
    assert round(f[0]) == 40 and round(f[1]) == 50 and round(f[4]) == 10 and round(f[-1]) == 40
    assert rankmodel.night_feats([], 1.0, 1e6, a1) == ([0.0, 0.0, 0.0, -3.0, 0.0, 0.0, 0.0, -3.0, 0.0], 1.0)
    assert rankmodel.night_feats([("2026-11-06T02:00:00+00:00", .1, .1, .1, .1, 9)], 1.0, 1e6, a1) is None   # 병합 등 가격 불일치


def _add_night(db, days, seed=2):
    """일봉 DB 에 밤사이 1분봉 추가: 데이마켓에 거래가 붙은 종목이 다음 날 정규장 고가 +45%."""
    rnd = random.Random(seed)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bars_1m (symbol TEXT, ts TEXT, trade_date TEXT, session TEXT, open REAL, high REAL, low REAL, "
                "close REAL, volume REAL, PRIMARY KEY (symbol, ts))")
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily_bars")]
    for prev, d in zip(days, days[1:]):
        _, a1, _ = rankmodel.night_window(prev, d)
        for s in syms:
            pc = con.execute("SELECT close FROM daily_bars WHERE symbol=? AND d=?", (s, prev)).fetchone()[0]
            hot = rnd.random() < 0.08
            con.execute("INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?)", (s, a1, d, "DAY", pc, pc * 1.02, pc, pc * 1.01, 5e5 if hot else 10))
            if hot:
                con.execute("UPDATE daily_bars SET high=? WHERE symbol=? AND d=?", (pc * 1.01 * 1.45, s, d))
    con.commit()
    con.close()


def test_train_night_score_and_snap(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    db, days = _db(tmp_path, n_days=120)
    _add_night(db, days)
    monkeypatch.setattr(rankmodel, "NIGHT_SINCE", days[70])
    now = datetime(2026, 5, 10, tzinfo=timezone.utc)
    m = rankmodel.train_night(db, now)
    assert m and m["features"] == list(rankmodel.FEATURES + rankmodel.NIGHT_FEATURES) and rankmodel.night_path(db).exists()
    assert rankmodel.night_check_text(m) == ""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    mq = lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()]
    sc = rankmodel.score_night(mq, db, days[-2], days[-1])
    hot = {r[0] for r in con.execute("SELECT symbol FROM bars_1m WHERE trade_date=? AND volume>1000", (days[-1],))}
    assert sc and sc[0]["symbol"] in hot and all("price" in r for r in sc)
    # 03:45 전엔 값 없음, 뒤엔 있음
    b1 = datetime.fromisoformat(rankmodel.night_window(days[-2], days[-1])[2])
    assert rankmodel.night_probs(db, days[-2], days[-1], b1 - timedelta(minutes=1)) == {}
    assert rankmodel.night_probs(db, days[-2], days[-1], b1 + timedelta(minutes=1))
    # 브리핑 때 보낸 후보 기록 → 실제 적중 채점
    rankmodel.save_snap(db, days[-1], sc)
    rankmodel.save_snap(db, days[-1], sc)                         # 같은 날 다시 쓰면 덮어씀
    lv = rankmodel._snap_score(db)
    assert lv and lv["days"] == 1 and lv["n"] == min(12, len(sc)) and lv["hit"] >= 1
    assert "브리핑 실제 적중" in rankmodel.night_check_text({"live": lv})
    con.close()


def test_briefing_text_night():
    b = {"date": "2026-11-09", "model": {"kind": "night", "check": "브리핑 실제 적중 최근 5일 40%"},
         "picks": [{"symbol": "ABC", "prob": 41.2, "tier": 0, "close": 1.0, "price": 1.35, "night": 35.0, "vol_ratio": 0, "labels": []}]}
    t = briefing.text(b)
    assert "+20% 더 갈 확률 순" in t and "ABC 41% $1.35 (밤사이 +35%)" in t and "실제 적중 최근 5일 40%" in t


# ---------------- 장중 모델 (5분마다 다시 매김)
def test_intra_feats_uses_only_past():
    d = "2026-11-06"
    _, a1, _ = rankmodel.night_window("2026-11-05", d)
    T = rankmodel._et(d, "07:00")
    bars = [(rankmodel._et(d, "05:00"), 1.0, 1.3, 1.0, 1.2, 1000), (rankmodel._et(d, "08:00"), 1.2, 9.0, 1.2, 5.0, 1000)]
    f, cur = rankmodel.intra_feats(bars, 1.0, 1e6, d, T, a1)
    assert cur == 1.2 and len(f) == len(rankmodel.INTRA_FEATURES) and max(f) < 200   # 08:00 봉(9달러)은 안 봄


def test_train_intra_and_live_probs(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    db, days = _db(tmp_path, n_days=120)
    _add_night(db, days)
    monkeypatch.setattr(rankmodel, "NIGHT_SINCE", days[70])
    now = datetime(2026, 5, 10, tzinfo=timezone.utc)
    m = rankmodel.train_intra(db, now)
    assert m and m["features"] == list(rankmodel.FEATURES + rankmodel.INTRA_FEATURES) and rankmodel.intra_path(db).exists()
    con = sqlite3.connect(db)
    hot = {r[0] for r in con.execute("SELECT symbol FROM bars_1m WHERE trade_date=? AND volume>1000", (days[-1],))}
    con.close()
    t = datetime.fromisoformat(rankmodel._et(days[-1], "06:02"))
    pr = rankmodel.intra_probs(db, days[-2], days[-1], t)
    assert pr and max(pr, key=pr.get) in hot
    assert rankmodel.intra_probs(db, days[-2], days[-1], datetime.fromisoformat(rankmodel._et(days[-1], "03:50"))) == {}
    assert rankmodel.intra_probs(db, days[-2], days[-1], datetime.fromisoformat(rankmodel._et(days[-1], "15:40"))) == {}
    assert rankmodel.live_probs(db, days[-2], days[-1], t) == pr
