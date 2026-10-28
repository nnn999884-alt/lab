from pennybot.trading import messages as msg
from pennybot.trading.notify import TelegramCommands


T = {"symbol": "PENX", "strategy": "runner_overnight", "stop": 1.11, "target": 1.38, "entry_price": 1.2345,
     "exit_price": 1.33, "remaining": 60, "trail_pct": 8.0, "qty": 120, "exited_qty": 120, "exit_reason": "익절",
     "pnl": 11.2, "pnl_pct": 7.6, "entry_session": "DAY", "exit_session": "PRE", "backstop_trigger": 1.07}


def test_first_line_names_event_and_symbol():
    # 잠금화면 미리보기에는 첫 줄만 보인다 — 첫 줄에 행동·종목·수량·가격·전략이 다 들어간다
    assert msg.buy_filled(T, 120, 1.2345, "DAY").split("\n")[0] == \
        "🟢 매수 완료 PENX · 120주 · $1.2345 · 나이트러너/데이마켓"
    assert msg.buy_filled(T, 50, 1.2345, "DAY", filled=50, planned=120).split("\n")[0] == \
        "🟢 매수 중 PENX · 50/120주 · $1.2345 · 나이트러너/데이마켓"
    assert msg.scaled_out(T, 60, 1.38).split("\n")[0] == "🟡 분할 익절 · PENX"
    assert msg.closed(T).split("\n")[0] == "🔵 청산 PENX · 익절"
    assert msg.closed({**T, "pnl": -5.0, "pnl_pct": -3.0, "exit_reason": "손절"}).startswith("🔴 청산 PENX · 손절")
    assert msg.backstop_fired(T).startswith("🛑 서버 손절 안전망 발동 · PENX")


def test_name_appended_when_known_omitted_when_not():
    assert msg.buy_filled(T, 120, 1.2345, "DAY", name="페닉스").split("\n")[0].startswith("🟢 매수 완료 PENX(페닉스) · 120주")
    assert msg.buy_filled(T, 120, 1.2345, "DAY").split("\n")[0].startswith("🟢 매수 완료 PENX · 120주")
    assert msg.scaled_out(T, 60, 1.38, name="페닉스").split("\n")[0] == "🟡 분할 익절 · PENX(페닉스)"
    assert msg.closed(T, name="페닉스").split("\n")[0] == "🔵 청산 PENX(페닉스) · 익절"
    assert msg.backstop_fired(T, name="페닉스").startswith("🛑 서버 손절 안전망 발동 · PENX(페닉스)")
    assert msg.sym_s("PENX", None) == "PENX" and msg.sym_s("PENX", "페닉스") == "PENX(페닉스)"


def test_formats():
    buy = msg.buy_filled(T, 120, 1.2345, "DAY")
    assert "총 $148.14" in buy and "나이트러너/데이마켓" in buy and "손절 $1.11" in buy
    assert len(buy.split("\n")) <= 3          # 휴대폰에서 스크롤 없이 읽히게
    c = msg.closed(T)
    assert "+$11.20 (+7.6%) · 120주" in c and "$1.2345 → $1.33" in c and "데이마켓→프리마켓" in c
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


def test_info_block_readable():
    stats = {"change_pct": 3.1, "volume": 3.2e6, "dollar": 3.9e6, "vol_ratio": 4.1, "ret_30": -45, "dd_60": -70, "range_30": 16}
    brief = {"short": {"ratio": 0.52}, "borrow": {"fee_rate": 45.0, "available": 15000}, "ssr": True,
             "filings": {"dilution_score": 28, "motives": [{"label": "ATM 진행 중"}, {"label": "상폐 기한 D-40"}]},
             "events": [{"ts": "2026-10-21T13:00:00+00:00", "summary": "$2.0M · 주당 $0.8000"}], "halts": []}
    L = msg.info_block(stats, brief, "30일 −45%, 데이마켓 전날 종가 근처")
    assert len(L) <= 3                     # 위험 → 오늘 성과 → 왜 샀는지, 3줄까지
    assert L[0] == "희석 28 · 공매도 52% · 대차 45% · SSR"
    assert L[1].startswith("📊 +3.1% · $3.9M · 평소 4.1배")
    assert L[2].startswith("30일 −45%")
    assert msg.info_block() == []
    full = msg.buy_filled(T, 120, 1.2345, "DAY", L)
    assert full.split("\n")[0].startswith("🟢 매수 완료 PENX · 120주") and "총 $148.14" in full
