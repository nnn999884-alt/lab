from collections import deque
from types import SimpleNamespace

from pennybot.collector.runner import Collector
from pennybot.sessions import PRE, REGULAR


def _col(session=REGULAR, shares=5e6):
    c = Collector.__new__(Collector)
    c._rt_shares = {"PEN": shares}
    c._rt_seeded = {"PEN"}
    c._rt_q = SimpleNamespace(items=[], put=lambda x: c._rt_q.items.append(x))
    c.cal = SimpleNamespace(classify=lambda now: (None, session))
    c.clock = lambda: None
    return c


def _win(t, p0, p1, vol):
    return deque([(t - 50, p0, vol / 2), (t, p1, vol / 2)])


def test_fast_fires_once_on_first_volume_burst():
    c = _col()
    t = 1_800_000_000.0
    m = int(t // 60)
    mins = {k: 1000.0 for k in range(m - 31, m - 1)}          # 평소 분당 1000주
    w = _win(t, 1.00, 1.03, 60000)                            # 60초 +3%, 6만 주(60배), 대금 $6만+
    c._fast_check("PEN", t, 1.03, w, mins, m)
    assert c._rt_q.items and c._rt_q.items[0][0] == "fast"
    c._fast_check("PEN", t + 5, 1.04, w, mins, m)             # 같은 날 두 번째는 안 보냄
    assert len(c._rt_q.items) == 1


def test_fast_skips_big_float_small_dollar_or_premarket():
    t = 1_800_000_000.0
    m = int(t // 60)
    mins = {k: 1000.0 for k in range(m - 31, m - 1)}
    for c, w in ((_col(shares=5e7), _win(t, 1.0, 1.03, 60000)),     # 주식 많음
                 (_col(), _win(t, 1.0, 1.03, 20000)),               # 대금 $2만
                 (_col(), _win(t, 1.0, 1.01, 60000)),               # +1% 뿐
                 (_col(session=PRE), _win(t, 1.0, 1.03, 60000))):   # 프리마켓
        c._fast_check("PEN", t, w[-1][1], w, mins, m)
        assert not c._rt_q.items
