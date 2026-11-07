from pennybot.collector.runner import Collector


class _Store:
    def __init__(self, bars):
        self.bars = bars

    def query(self, sql, args=()):
        return self.bars


def _col():
    return Collector.__new__(Collector)


def test_chase_line_warns_after_big_run():
    # 저점 1.00 → 지금 1.15 (+15%), 저점 뒤 양봉 3개
    bars = [(1.05, 1.02, 1.03), (1.03, 1.00, 1.01), (1.01, 1.01, 1.05), (1.05, 1.04, 1.10), (1.10, 1.09, 1.15)]
    c = _col()
    run, greens = c._run_up(_Store(bars), "PEN", 1.15)
    assert round(run) == 15 and greens == 3
    assert "추격 주의" in c._chase_line(_Store(bars), "PEN", 1.15)


def test_chase_line_silent_when_early_or_no_bars():
    bars = [(1.0, 0.99, 1.0), (1.0, 1.0, 1.02), (1.02, 1.01, 1.03)]
    c = _col()
    assert c._chase_line(_Store(bars), "PEN", 1.03) == ""
    assert c._chase_line(_Store([]), "PEN", 1.03) == ""
