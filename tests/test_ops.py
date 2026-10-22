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


def test_loss_streak_cooldown_and_weekly_limit(tmp_path):
    from datetime import timedelta

    from pennybot.config import TradingConfig
    from pennybot.trading.risk import Risk
    t = TradingConfig(max_daily_loss_usd=1e9, max_weekly_loss_usd=0, loss_streak_stop=3)
    j = Journal(tmp_path / "r.db")
    risk = Risk(t, j, Control(tmp_path))
    base = et(5, 0)

    def trade(i, exit_px, when):
        tid = j.open_trade(strategy="runner_overnight", symbol=f"S{i}", trade_date="2026-10-21", planned_qty=10,
                           stop=0.5, target=2, sell_from=base, deadline=base, reason="", now=base)
        j.record_entry_fill(tid, 10, 1.0, "DAY", when)
        j.record_exit_fill(tid, 10, exit_px, "PRE", when)

    for i in range(3):
        trade(i, 0.9, base + timedelta(minutes=i))
    assert "연속 손실" in risk.can_enter("runner_overnight", "NEW", "2026-10-22", [], now=base + timedelta(hours=1))
    assert risk.can_enter("runner_overnight", "NEW", "2026-10-22", [], now=base + timedelta(hours=25)) is None
    trade(9, 1.2, base + timedelta(minutes=9))           # 이익 한 번이면 연속 끊김
    assert risk.can_enter("runner_overnight", "NEW", "2026-10-22", [], now=base + timedelta(hours=1)) is None
    t.max_weekly_loss_usd = 2                            # 7일 순손실 ≈ −1$ (−3 + 2) → 한도 2$ 안
    assert risk.cooldown(base + timedelta(hours=1)) is None
    t.max_weekly_loss_usd = 0.5
    assert "7일" in risk.cooldown(base + timedelta(hours=1))
    assert risk.cooldown(base + timedelta(days=8)) is None


def test_toss_warning_blocks_entry(env):
    tr, market, mv, clock, j, ctl, ex = env
    mv.warnings = lambda sym: ["VI_DYNAMIC"] if sym == "PEN" else []
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    assert not j.q("SELECT * FROM trades")
    assert any("VI_DYNAMIC" in r["reason"] for r in j.q("SELECT reason FROM signals"))


def test_market_view_warnings_active_only(tmp_path):
    from datetime import datetime, timezone

    from pennybot.sessions import SessionCalendar
    from pennybot.trading.market import MarketView

    class C:
        calls = 0

        def warnings(self, sym):
            C.calls += 1
            return [{"warningType": "INVESTMENT_WARNING", "startDate": "2026-10-20", "endDate": None},
                    {"warningType": "VI_STATIC", "startDate": "2026-10-01", "endDate": "2026-10-02"}]
    now = [datetime(2026, 10, 22, 3, tzinfo=timezone.utc)]
    mv = MarketView(":memory:", C(), SessionCalendar(), None, clock=lambda: now[0])
    assert mv.warnings("X") == ["INVESTMENT_WARNING"]
    mv.warnings("X")
    assert C.calls == 1                                  # 5분 캐시
