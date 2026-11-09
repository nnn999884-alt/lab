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
