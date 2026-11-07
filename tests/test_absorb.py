import sqlite3

from pennybot.absorb import AbsorbTracker, TickLog, absorb_text


def test_absorb_fires_when_bid_holds_and_eats_more_than_shown():
    a = AbsorbTracker(min_x=3, min_dollar=1000, min_hold=10)
    a.book("X", 0.0, 0.50, 1000)                       # 매수 1호가 $0.50 잔량 1,000주
    assert a.trade("X", 5.0, 0.50, 2000) is None       # 아직 10초 안 됨
    a.book("X", 6.0, 0.50, 800)                        # 같은 가격 잔량만 바뀜 → 이어서 셈
    ev = a.trade("X", 12.0, 0.50, 1500)                # 누적 3,500주 = 보이던 최대 1,000의 3.5배, $1,750
    assert ev and ev["price"] == 0.50 and round(ev["x"], 1) == 3.5 and ev["dollar"] == 1750
    assert a.trade("X", 13.0, 0.50, 100) is None       # 한 번 울리면 처음부터 다시 셈


def test_absorb_resets_when_bid_breaks_or_moves():
    a = AbsorbTracker(min_x=3, min_dollar=100, min_hold=0)
    a.book("X", 0.0, 0.50, 1000)
    a.trade("X", 1.0, 0.50, 2500)
    assert a.trade("X", 2.0, 0.49, 100) is None        # 매수호가 아래 체결 = 받침 깨짐 → 초기화
    assert a.trade("X", 3.0, 0.50, 5000) is None       # 호가 다시 안 들어왔으니 판정 안 함
    a.book("X", 4.0, 0.50, 1000)
    a.book("X", 5.0, 0.51, 1000)                       # 가격이 올라감 → 새 가격부터
    assert a.trade("X", 6.0, 0.51, 2000) is None
    assert a.trade("X", 7.0, 0.52, 9000) is None       # 매도호가 쪽(사는 쪽) 체결은 안 셈


def test_absorb_window_drops_old_hits():
    a = AbsorbTracker(window=60, min_x=3, min_dollar=100, min_hold=0)
    a.book("X", 0.0, 1.0, 1000)
    a.trade("X", 1.0, 1.0, 2500)
    assert a.trade("X", 100.0, 1.0, 600) is None        # 앞 체결은 60초 밖


def test_ticklog_writes_and_text(tmp_path):
    p = tmp_path / "ticks.db"
    import time
    now = time.time()                                     # 30일 지난 기록은 flush 때 지워지므로 지금 시각으로
    t = TickLog(p, book_every=1.0)
    t.add_trade(now, "X", 0.5, 100)
    t.add_book(now, "X", "[[0.5, 100]]", "[[0.51, 50]]")
    t.add_book(now + 0.5, "X", "[[0.5, 90]]", "[[0.51, 50]]")   # 1초 안 → 건너뜀
    t.add_trade(now - 40 * 86400, "X", 0.5, 100)              # 40일 전 → 정리됨
    ev = {"price": 0.5, "hit_vol": 4000, "dollar": 2000, "shown": 1000, "x": 4.0, "held": 30}
    t.add_absorb(now + 1, "X", ev)
    assert t.flush() == 4
    db = sqlite3.connect(p)
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM books").fetchone()[0] == 1
    assert db.execute("SELECT x FROM absorb").fetchone()[0] == 4.0
    msg = absorb_text("X", "Xco", ev, 0.5, "프리마켓", 0.12)
    assert "물량 흡수" in msg and "4배" in msg and "0.12%" in msg
