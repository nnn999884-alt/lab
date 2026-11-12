import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pennybot.collector.runner import Collector
from pennybot.precursor import early_watch
from pennybot.sessions import PRE, REGULAR

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


def _bars(n, jump_at=None, vol=100.0, jump_vol=1000.0):
    out = []
    for i in range(n):
        ts = (T0 + timedelta(minutes=i)).isoformat()
        if jump_at is not None and i >= jump_at:
            out.append((ts, 1.0, 1.06, 1.0, 1.05, jump_vol))   # 10분 전 시가 1.0 → 종가 1.05 (+5%) 양봉
        else:
            out.append((ts, 1.0, 1.01, 0.99, 1.0, vol))
    return out


def test_win60_drops_old_trades():
    from collections import deque
    from pennybot.collector.runner import win60
    w = {"A": deque([(100.0, 2.0, 10), (150.0, 2.0, 5), (170.0, 3.0, 1)]), "B": deque([(50.0, 1.0, 9)])}
    out = win60(w, 200.0)
    assert out["A"] == (6.0, 13.0) and out["B"] == (0.0, 0.0)


def test_early_watch_first_hit():
    assert early_watch(_bars(20), 1_000_000) is None
    i, up, turn = early_watch(_bars(20, jump_at=15), 1_000_000)
    assert i == 15 and round(up, 1) == 5.0 and round(turn, 2) == 0.10
    assert early_watch(_bars(20, jump_at=15, jump_vol=100), 1_000_000) is None   # 회전율 0.01% → 안 됨
    assert early_watch(_bars(20, jump_at=15), None) is None


class _Store:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("CREATE TABLE events (ts TEXT, symbol TEXT, kind TEXT, text TEXT, value REAL);"
                              "CREATE TABLE filings (symbol TEXT, form TEXT, accepted TEXT);"
                              "CREATE TABLE news (symbol TEXT, published TEXT);")

    def query(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()


def _fake(store):
    sent = []
    f = SimpleNamespace(store=store, universe={"ABC": {"name": "Abc"}}, sent=sent,
                        notifier=SimpleNamespace(send=sent.append), _on=lambda k: True,
                        _chase_line=lambda store, sym, price: "")
    f._gsend = lambda sym, price, text, **k: sent.append(text) or True       # 등급은 test_alertgrade 에서
    f._event = lambda now, sym, kind, text, value=None, store=None: store.db.execute(
        "INSERT INTO events VALUES (?,?,?,?,?)", (now.isoformat(), sym, kind, text, value))
    return f


def test_early_watch_needs_reason_and_fires_once():
    st = _Store(); f = _fake(st)
    bars = _bars(18, jump_at=16)
    now = T0 + timedelta(minutes=18)
    start = T0.isoformat()
    # 점수 낮고 공시 없음 → 안 보냄
    assert not Collector._early_watch(f, now, PRE, "ABC", 5, bars, 1_000_000, start)
    # 정규장은 점수 높아도 안 보냄 (검증에서 점수 효과 없음)
    assert not Collector._early_watch(f, now, REGULAR, "ABC", 10, bars, 1_000_000, start)
    # 프리장 점수 9↑ → 보냄, 같은 날 두 번은 안 보냄
    assert Collector._early_watch(f, now, PRE, "ABC", 9, bars, 1_000_000, start)
    assert len(f.sent) == 1 and "초기 경보" in f.sent[0] and "점수 9" in f.sent[0]
    assert not Collector._early_watch(f, now, PRE, "ABC", 9, bars, 1_000_000, start)


def test_early_watch_recent_filing_any_session_and_stale_hit():
    st = _Store(); f = _fake(st)
    now = T0 + timedelta(minutes=18)
    st.db.execute("INSERT INTO filings VALUES ('ABC','8-K',?)", ((now - timedelta(minutes=30)).isoformat(),))
    # 처음 맞은 봉이 오래전(최근 3분 밖)이면 안 보냄
    assert not Collector._early_watch(f, now, REGULAR, "ABC", 0, _bars(18, jump_at=12), 1_000_000, T0.isoformat())
    assert Collector._early_watch(f, now, REGULAR, "ABC", 0, _bars(18, jump_at=16), 1_000_000, T0.isoformat())
    assert "공시 8-K" in f.sent[0]
