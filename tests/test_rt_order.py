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
