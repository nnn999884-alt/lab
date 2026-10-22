"""알림 문구 틀. 모든 알림이 같은 모양이 되게 한다:

  <아이콘> <제목> · <종목>
  <핵심 숫자 한 줄>
  <보조 정보 한두 줄>
  → <할 일> (경고만)

휴대폰 잠금화면 미리보기에 첫 줄만 보이므로 첫 줄에 무슨 일인지·어느 종목인지를 넣는다.
"""
from __future__ import annotations

from .report import STRAT
from .strategies import SESSION_KO


def usd(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "-"
    s = "+" if sign and v > 0 else "−" if v < 0 else ""
    return f"{s}${abs(v):,.2f}"


def px(v: float | None) -> str:
    """$1 미만은 소수점 4자리, 그 이상은 2~4자리 (1.2345 → $1.2345, 1.33 → $1.33)."""
    if v is None:
        return "-"
    if v < 1:
        return f"${v:.4f}"
    s = f"{v:.4f}".rstrip("0")
    return "$" + (s + "0" * (2 - len(s.split(".")[1])))


def pct(v: float | None) -> str:
    return "-" if v is None else f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.1f}%"


def _strat(t: dict) -> str:
    return STRAT.get(t.get("strategy"), t.get("strategy") or "-")


def _sess(s: str | None) -> str:
    return SESSION_KO.get(s, s or "-")


def buy_filled(t: dict, qty: int, price: float, session: str) -> str:
    lines = [f"🟢 매수 체결 · {t['symbol']}",
             f"{qty}주 × {px(price)} = {usd(qty * price)}",
             f"{_strat(t)} · {_sess(session)}"]
    if t.get("stop") or t.get("target"):
        lines.append(f"손절 {px(t.get('stop'))} · 익절 {px(t.get('target'))}")
    return "\n".join(lines)


def scaled_out(t: dict, qty: int, price: float) -> str:
    gain = (price / t["entry_price"] - 1) * 100 if t.get("entry_price") else None
    return "\n".join([
        f"🟡 분할 익절 · {t['symbol']}",
        f"{qty}주 × {px(price)} ({pct(gain)})",
        f"나머지 {t['remaining']}주: 손절을 본전 {px(t.get('entry_price'))}로, 고점 대비 −{t.get('trail_pct') or 0:g}% 트레일링",
    ])


def closed(t: dict) -> str:
    pnl = t.get("pnl") or 0.0
    icon = "🔵" if pnl >= 0 else "🔴"
    lines = [f"{icon} 청산 · {t['symbol']} · {t.get('exit_reason') or '-'}",
             f"{usd(pnl, sign=True)} ({pct(t.get('pnl_pct'))}) 수수료 뺀 순손익",
             f"{px(t.get('entry_price'))} → {px(t.get('exit_price'))} · {t.get('exited_qty') or t.get('qty') or 0}주",
             f"{_strat(t)} · {_sess(t.get('entry_session'))} → {_sess(t.get('exit_session'))}"]
    return "\n".join(lines)


def backstop_fired(t: dict) -> str:
    return "\n".join([f"🛑 서버 손절 안전망 발동 · {t['symbol']}",
                      f"{t.get('remaining') or 0}주, 발동가 {px(t.get('backstop_trigger'))}",
                      "토스 서버가 손절 주문을 냈습니다. 봇이 이어서 체결을 추적합니다"])


def warn(title: str, detail: str = "", action: str = "") -> str:
    """경고: 첫 줄 제목, 둘째 줄 상황, 마지막 줄 할 일."""
    return "\n".join(x for x in (f"⚠️ {title}", detail, f"→ {action}" if action else "") if x)


def started(mode: str, link: str | None = None) -> str:
    lines = [f"🤖 매매 봇 시작 · {'실거래' if mode == 'live' else '모의매매'}", "/상태 · /리포트 · /대시보드 · /도움"]
    if link:
        lines.append(f"📊 {link}")
    return "\n".join(lines)


def with_link(text: str, link: str | None) -> str:
    return f"{text}\n\n📊 대시보드: {link}" if link else text
