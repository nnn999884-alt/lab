from pennybot.trading import messages as msg
from pennybot.trading.notify import TelegramCommands


T = {"symbol": "PENX", "strategy": "runner_overnight", "stop": 1.11, "target": 1.38, "entry_price": 1.2345,
     "exit_price": 1.33, "remaining": 60, "trail_pct": 8.0, "qty": 120, "exited_qty": 120, "exit_reason": "익절",
     "pnl": 11.2, "pnl_pct": 7.6, "entry_session": "DAY", "exit_session": "PRE", "backstop_trigger": 1.07}


def test_first_line_names_event_and_symbol():
    # 잠금화면 미리보기에는 첫 줄만 보인다
    assert msg.buy_filled(T, 120, 1.2345, "DAY").split("\n")[0] == "🟢 매수 체결 · PENX"
    assert msg.scaled_out(T, 60, 1.38).split("\n")[0] == "🟡 분할 익절 · PENX"
    assert msg.closed(T).split("\n")[0] == "🔵 청산 · PENX · 익절"
    assert msg.closed({**T, "pnl": -5.0, "pnl_pct": -3.0, "exit_reason": "손절"}).startswith("🔴 청산 · PENX · 손절")
    assert msg.backstop_fired(T).startswith("🛑 서버 손절 안전망 발동 · PENX")


def test_formats():
    buy = msg.buy_filled(T, 120, 1.2345, "DAY")
    assert "120주 × $1.2345 = $148.14" in buy and "러너 오버나이트 · 데이마켓" in buy and "손절 $1.11" in buy
    c = msg.closed(T)
    assert "+$11.20 (+7.6%)" in c and "$1.2345 → $1.33 · 120주" in c and "데이마켓 → 프리마켓" in c
    assert msg.usd(-3.5, sign=True) == "−$3.50" and msg.px(12.3) == "$12.30" and msg.pct(None) == "-"
    w = msg.warn("수집기 멈춤", "12분째", "창 확인")
    assert w == "⚠️ 수집기 멈춤\n12분째\n→ 창 확인"
    assert msg.warn("제목") == "⚠️ 제목"
    assert msg.with_link("본문", None) == "본문" and msg.with_link("본문", "https://x").endswith("📊 대시보드: https://x")


class _Trader:
    url = None
    def dashboard_url(self): return self.url


def test_dashboard_command():
    tr = _Trader()
    tc = TelegramCommands("tok", "1", control=None, trader=tr, notifier=None)
    assert "꺼져" in tc.handle("/대시보드")
    tr.url = "https://abc.trycloudflare.com"
    assert tc.handle("/dashboard") == "📊 대시보드: https://abc.trycloudflare.com"
    assert "/대시보드" in tc.handle("/도움")
