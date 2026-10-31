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
    assert "오늘: 1건" in rep and "PEN" in rep and f"+${t['pnl']:,.2f}" in rep and "[데이→프리]" in rep
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


def test_watchdog_waits_after_startup(env):
    tr, market, mv, clock, j, ctl, ex = env
    from datetime import timedelta
    assert not tr._due("watchdog", tr.clock(), timedelta(minutes=5))      # 켜자마자는 감시 안 함
    assert tr._due("watchdog", tr.clock() + timedelta(minutes=5), timedelta(minutes=5))


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
    t.strategies["runner_overnight"]["enabled"] = True
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


def test_daily_report_waits_for_morning_kst_and_sends_once(env, tmp_path):
    from datetime import timedelta
    tr, market, mv, clock, j, ctl, ex = env
    tr.reports_dir = str(tmp_path / "reports")
    sent = []
    tr.notifier.send = lambda text, **kw: sent.append(text)
    j.set_meta("last_report", "2026-10-20")
    market.set("PEN", 1.03, 1.035)
    tr.tick()
    clock[0] = et(9, 1)
    market.set("PEN", 1.07, 1.08)
    tr.tick()                                          # 프리마켓에 청산
    sent.clear()
    clock[0] = et(17, 0)                               # 정규장 끝 (한국 06:00) → 07:30 까지 기다림
    tr.daily_report(clock[0])
    assert not sent
    clock[0] = et(18, 31)                              # 한국 07:31
    tr.daily_report(clock[0])
    tr.daily_report(clock[0] + timedelta(minutes=5))
    assert len(sent) == 1 and "일일 리포트 2026-10-21" in sent[0] and "PEN" in sent[0]
    assert (tmp_path / "reports" / "2026-10-21.txt").read_text(encoding="utf-8") == sent[0]
    assert j.get_meta("last_report") == "2026-10-21"


def test_daily_report_catches_up_after_bot_was_off(env):
    from datetime import timedelta
    tr, market, mv, clock, j, ctl, ex = env
    sent = []
    tr.notifier.send = lambda text, **kw: sent.append(text)
    j.set_meta("last_report", "2026-10-20")
    clock[0] = et(21, 0) + timedelta(days=0)           # 다음 날 데이마켓에 켬 (한국 10:00)
    tr.cal.classify(clock[0])
    tr.daily_report(clock[0])
    assert len(sent) == 1 and "2026-10-21" in sent[0] and "매매 없음" in sent[0]


def test_telegram_report_with_date_and_empty_message(tmp_path):
    class T:
        def report_text(self, d=None): return f"리포트 {d}"
    tg = TelegramCommands("t", "1", Control(tmp_path), T(), Notifier(background=False))
    assert tg.handle("/리포트 2026-10-21") == "리포트 2026-10-21"
    assert tg.handle("/report") == "리포트 None"
    assert tg.handle("") is None


def test_secrets_not_logged_and_long_messages_split(monkeypatch, caplog):
    import requests

    from pennybot.trading import notify
    tok = "123456:SECRET-TOKEN"
    hook = "https://discord.com/api/webhooks/1/SECRET-HOOK"
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(notify.requests, "post", post)
    n = Notifier(hook, tok, "42", background=False)
    n.send("x\n" * 3000)                              # 6000자 → 텔레그램 2조각, 디스코드 4조각
    assert sum(1 for u, _ in calls if "telegram" in u) == 2
    assert sum(1 for u, _ in calls if "discord" in u) == 4
    assert all(len(j.get("text", "")) <= 4000 for _, j in calls)
    assert "SECRET" not in caplog.text and "***" in caplog.text


def test_getupdates_conflict_raises(tmp_path):
    class S:
        def get(self, *a, **k): return Resp({"ok": False, "error_code": 409, "description": "Conflict"})
    tg = TelegramCommands("t", "1", Control(tmp_path), FakeTrader(), Notifier(background=False), session=S())
    import pytest
    with pytest.raises(RuntimeError, match="409"):
        tg.poll(timeout=0)
