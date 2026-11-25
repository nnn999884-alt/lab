import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pennybot2.core.sessions import ET
from pennybot2.core.store import Store
from pennybot2.hub.client import HubClient
from pennybot2.hub.server import Hub
from pennybot2.scoring import nightscore
from pennybot2.workers import daily
from pennybot2.workers.surge import enrich


def synth(seed, n=80, start=1.0):
    rnd, p, rows = random.Random(seed), start, []
    d0 = datetime(2026, 1, 5)
    for i in range(n):
        o = p
        c = max(0.02, p * (1 + rnd.uniform(-0.15, 0.2)))
        h, l = max(o, c) * (1 + rnd.random() * 0.1), min(o, c) * (1 - rnd.random() * 0.1)
        rows.append(((d0 + timedelta(days=i)).date().isoformat(), o, h, l, c, rnd.uniform(1e5, 5e6)))
        p = c
    return rows


def test_nightscore_matches_old_implementation():
    root = str(Path(__file__).resolve().parents[2])
    sys.path.insert(0, root)
    try:
        from pennybot import nightscore as old
    except Exception:
        pytest.skip("기존 pennybot 을 불러올 수 없음")
    finally:
        sys.path.remove(root)
    checked = 0
    for seed in range(60):
        rows = synth(seed, start=random.Random(seed).uniform(0.1, 6))
        assert nightscore.score(rows) == old.score(rows)
        checked += nightscore.score(rows) is not None
    assert checked > 10                                    # 비교가 의미 있게 점수가 나온 경우가 충분히 있음


def test_score_none_when_short_or_out_of_range():
    assert nightscore.score(synth(1, n=30)) is None
    assert nightscore.score(synth(1, n=80, start=500)) is None


CHART = {"chart": {"result": [{"timestamp": [1764000000, 1764086400, 1764172800],
         "indicators": {"quote": [{"open": [1.0, None, 1.2], "high": [1.1, 1.2, 1.3], "low": [0.9, 1.0, 1.1],
                                   "close": [1.05, 1.1, 1.25], "volume": [1000, 2000, None]}]}}]}}


def test_parse_chart_skips_missing_and_filters_since():
    rows = daily.parse_chart(CHART, "2000-01-01")
    assert len(rows) == 2 and rows[1][5] == 0.0            # 값이 빠진 날 제외, 거래량 None → 0
    assert daily.parse_chart(CHART, "2999-01-01") == []
    assert daily.parse_chart({}, "2000-01-01") == []
    assert daily.parse_chart({"chart": {"result": None}}, "2000-01-01") == []


def test_last_completed_day_handles_close_weekend_holiday():
    at = lambda *a: datetime(*a, tzinfo=ET)
    assert daily.last_completed_day(at(2026, 11, 25, 17, 0)) == "2026-11-25"
    assert daily.last_completed_day(at(2026, 11, 25, 15, 0)) == "2026-11-24"
    assert daily.last_completed_day(at(2026, 11, 26, 12, 0)) == "2026-11-25"      # 추수감사절
    assert daily.last_completed_day(at(2026, 11, 27, 12, 0)) == "2026-11-25"      # 조기폐장 당일 13:20 전
    assert daily.last_completed_day(at(2026, 11, 27, 13, 30)) == "2026-11-27"
    assert daily.last_completed_day(at(2026, 11, 29, 10, 0)) == "2026-11-27"      # 일요일


def test_sync_and_candidates(monkeypatch):
    s = Store(":memory:")
    s.put_daily([("OLD", "2026-11-20", 1, 1, 1, 1, 1)])
    calls = []

    def fake(session, sym, since, rng):
        calls.append((sym, rng))
        return [(r[0], *r[1:]) for r in synth({"NEW": 1, "OLD": 2}.get(sym, 3))][-70:] if sym != "BAD" else (_ for _ in ()).throw(RuntimeError("x"))

    monkeypatch.setattr(daily, "fetch_symbol", fake)
    res = daily.sync(s, ["OLD", "NEW", "BAD"], "2026-11-24", workers=2, pause=0)
    assert res["failed"] == 1 and res["requested"] == 3
    assert sorted(calls) == [("BAD", "6mo"), ("NEW", "6mo"), ("OLD", "10d")]       # 이력 있으면 10일치
    assert daily.stale(s, ["NEW"], "0000") == []                                   # 이미 받은 종목은 다시 안 받음
    asof, items = daily.build_candidates(s, ["NEW", "OLD", "BAD"], min_score=-99)
    assert asof and {i["s"] for i in items} == {"NEW", "OLD"}                       # BAD 는 일봉이 없어 제외
    assert [i["score"] for i in items] == sorted((i["score"] for i in items), reverse=True)   # 점수 높은 순
    s.put_candidates(asof, items)
    assert s.db.execute("SELECT count(*) FROM candidates").fetchone()[0] == 2


def test_hub_retains_candidates_for_late_subscriber(tmp_path):
    h = Hub(tmp_path / "m.db", port=0, flush_sec=0.2).start_in_thread()
    pub = HubClient(h.port)
    pub.connected.wait(3)
    pub.publish("candidates", {"asof": "2026-11-24", "items": [{"s": "ABC", "score": 6, "why": ["a"], "minus": []}]})
    time.sleep(0.2)
    got = []
    HubClient(h.port, ["candidates"], lambda t, d: got.append(d))        # 나중에 붙은 구독자
    end = time.time() + 3
    while time.time() < end and not got:
        time.sleep(0.02)
    assert got and got[0]["items"][0]["s"] == "ABC"


def test_surge_signal_enriched_with_candidate():
    sig = {"s": "ABC", "ts": 0, "kind": "surge", "value": 12.0, "text": "ABC 5분 안에 +12.0% ($1.1000)"}
    out = enrich(sig, {"s": "ABC", "score": 6, "why": ["전날 +10%↑", "$0.5 미만"], "minus": []})
    assert "후보 6점" in out["text"] and out["candidate_score"] == 6 and "후보" not in sig["text"]
    assert enrich(sig, None) is sig
