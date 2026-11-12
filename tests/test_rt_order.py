import time
from types import SimpleNamespace

from pennybot.collector.runner import Collector


def _col(max_symbols=4):
    c = Collector.__new__(Collector)
    c._rt = SimpleNamespace(max_symbols=max_symbols, pinned=None, books=None)
    c._rt.pin = lambda w: setattr(c._rt, "pinned", list(w))
    c._rt.pin_books = lambda w: setattr(c._rt, "books", list(w))
    c._rt_seeded, c._rt_q = set(), SimpleNamespace(put=lambda x: None)
    c._dna = (None, [])
    c.cfg = SimpleNamespace()
    return c


def test_recent_ranked_and_active_first_quiet_pushed_out():
    c = _col()
    now = time.time()
    m = int(now // 60)
    c._fast_hot = ("d", {"OLD", "NEW"})
    c._fast_seen = {"OLD": now - 3600, "NEW": now}            # OLD 은 1시간 전에만 순위에 있었음
    c._rt_syms = {"QUIET", "HOT", "OLD"}
    c._rt_dmin = {"HOT": {m: 5e4}, "QUIET": {m - 8: 9e4}}      # QUIET 은 8분 전에만 거래
    c._rt_set(["QUIET", "W1", "W2", "HOT"])
    assert c._rt.pinned[:2] == ["NEW", "HOT"]                  # 방금 순위 → 지금 체결 붙는 종목
    assert "QUIET" not in c._rt.pinned and "OLD" not in c._rt.pinned   # 식은 종목은 칸 밖 → 1분 조회로


def test_promoted_without_money_gives_slot_back():
    """잡힌 지 2분 넘었는데 웹소켓 대금이 안 붙은 종목은 칸을 비우고, 돈이 붙은 종목·방금 잡힌 종목이 앞에."""
    c = _col(max_symbols=3)
    now = time.time()
    m = int(now // 60)
    c._fast_hot = ("d", {"DEAD", "LIVE", "JUST"})
    c._fast_seen = {"DEAD": now - 300, "LIVE": now - 300, "JUST": now}
    c._rt_syms = {"DEAD", "LIVE"}
    c._rt_dmin = {"LIVE": {m: 3e4}, "DEAD": {m: 10.0}}
    c._rt_set(["W1", "W2"])
    assert c._rt.pinned[:2] == ["JUST", "LIVE"]
    assert "DEAD" not in c._rt.pinned


def test_trial_money_bar_differs_by_session(tmp_path):
    """구독 뒤 2분 웹소켓 대금 기준은 세션별 (데이마켓 $1천 · 정규 $5천, 설정 탭에서 바꿈)."""
    from datetime import datetime, timezone
    from pennybot import settings
    c = _col(max_symbols=3)
    c._settings = settings.Watcher(str(tmp_path))
    day = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)       # 22:00 ET 데이마켓
    reg = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)      # 11:00 ET 정규장
    assert c._sweep_min_dollar(day) == 1000 and c._sweep_min_dollar(reg) == 5000
    settings.save(str(tmp_path), {"sweep_min_dollar": {"DAY": 2500}})
    assert c._sweep_min_dollar(day) == 2500 and c._sweep_min_dollar(reg) == 5000


def test_universe_filter_mcap_and_adv_keeps_recent_runners(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from pennybot import settings
    c = _col()
    c._settings = settings.Watcher(str(tmp_path))
    data = {"stock_info": [("BIG", 4e8), ("SMALL", 5e6), ("RUN", 4e8), ("THIN", 5e6)],
            "dates": [("2026-11-11",)], "adv": [("BIG", 1e6), ("SMALL", 1e5), ("RUN", 1e6), ("THIN", 1e3)],
            "runners": [("RUN",)]}
    def q(sql, a=()):
        if "stock_info" in sql: return data["stock_info"]
        if "DISTINCT d" in sql: return data["dates"]
        if "AVG" in sql: return data["adv"]
        return data["runners"]
    c.store = SimpleNamespace(query=q)
    uni = {s: {"last": 2.0} for s in ("BIG", "SMALL", "RUN", "THIN", "NEW")}
    out = c._universe_filter(uni, datetime(2026, 11, 12, tzinfo=timezone.utc))
    assert sorted(out) == ["NEW", "RUN", "SMALL"]     # BIG 시총 $8억 · THIN 거래 없음 제외, RUN 은 최근 급등주라 남김, NEW 는 모름 → 남김
