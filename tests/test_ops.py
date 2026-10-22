from pennybot.trading import Control, Journal
from pennybot.trading.notify import Notifier, TelegramCommands

from test_trading import et


def test_notifier_throttles_same_key():
    sent = []
    clock = [0.0]
    n = Notifier("http://hook", clock=lambda: clock[0], background=False)
    n._deliver = sent.append
    for _ in range(5):
        n.send("오류", key="e", every=600)
    clock[0] = 601
    n.send("오류", key="e", every=600)
    n.send("체결")
    assert sent == ["오류", "오류", "체결"]


class FakeTrader:
    def status_text(self): return "상태 OK"
    def report_text(self): return "리포트"


class Resp:
    def __init__(self, body): self.body = body
    def json(self): return self.body


class Sess:
    def __init__(self, updates): self.updates, self.params = updates, []
    def get(self, url, params=None, timeout=None):
        self.params.append(params)
        out, self.updates = self.updates, []
        return Resp({"ok": True, "result": out})


def test_telegram_commands_only_from_my_chat(tmp_path):
    ctl = Control(tmp_path)
    sent = []
    n = Notifier(background=False)
    n.send = lambda text, **kw: sent.append(text)
    msgs = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/정지"}},          # 남의 chat → 무시
            {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/상태"}},
            {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/정지"}},
            {"update_id": 4, "message": {"chat": {"id": 42}, "text": "/청산"}},
            {"update_id": 5, "message": {"chat": {"id": 42}, "text": "/청산 확인"}}]
    s = Sess(msgs)
    tg = TelegramCommands("tok", "42", ctl, FakeTrader(), n, session=s)
    tg.poll(timeout=0)
    d = ctl.load()
    assert d["kill"] and d["flatten"]
    assert sent[0] == "상태 OK" and "확인" in sent[2]
    tg.poll(timeout=0)
    assert s.params[-1]["offset"] == 6                  # 처리한 메시지는 다시 받지 않음
    assert tg.handle("/재개").startswith("▶️") and not ctl.load()["kill"]


def test_fees_in_pnl_and_daily_report(env):
    tr, market, mv, clock, j, ctl, ex = env
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    clock[0] = et(9, 1)
    market.set("PEN", 1.07, 1.08)
    tr.tick()
    t = j.q("SELECT * FROM trades")[0]
    gross = (t["exit_price"] - t["entry_price"]) * t["qty"]
    fees = t["qty"] * (t["entry_price"] + t["exit_price"]) * 0.001
    assert abs(t["fees"] - fees) < 1e-9 and abs(t["pnl"] - (gross - fees)) < 1e-9
    rep = tr.report_text("2026-10-21")
    assert "1건" in rep and "PEN" in rep and f"{t['pnl']:+.2f}$" in rep
    assert "보유 종목 없음" in tr.status_text()


def test_watchdog_alerts_when_collector_stale(env):
    tr, market, mv, clock, j, ctl, ex = env
    sent = []
    tr.notifier.send = lambda text, **kw: sent.append(text)
    from datetime import timedelta
    td, sess = tr.cal.classify(clock[0])
    mv.last_collect_ts = lambda: clock[0] - timedelta(minutes=30)
    tr.watchdog(clock[0], td, sess)
    assert sent and "30분" in sent[0]
    sent.clear()
    mv.last_collect_ts = lambda: clock[0] - timedelta(minutes=1)
    tr.watchdog(clock[0], td, sess)
    assert not sent


def test_journal_migrates_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, mode TEXT, status TEXT)")
    c.commit()
    c.close()
    j = Journal(db)
    cols = {r["name"] for r in j.q("PRAGMA table_info(trades)")}
    assert {"fees", "backstop_id", "scaled", "trail_pct"} <= cols
