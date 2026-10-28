"""알림 문구 틀. 모든 알림이 같은 모양이 되게 한다:

  <아이콘> <제목> · <종목>(<종목명>)
  <핵심 숫자 한 줄>
  <보조 정보 한두 줄>
  → <할 일> (경고만)

휴대폰 잠금화면 미리보기에 첫 줄만 보이므로 첫 줄에 무슨 일인지·어느 종목인지를 넣는다.
종목명은 모를 수 있으니(name=None) 그때는 괄호를 통째로 뺀다.
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


def sym_s(symbol: str, name: str | None = None) -> str:
    return f"{symbol}({name})" if name else symbol


def qty_s(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3 else f"{v:,.0f}"


def money_s(v: float | None) -> str:
    if v is None:
        return "-"
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K" if v >= 1e3 else f"${v:,.0f}"


def _held(t: dict) -> str | None:
    from datetime import datetime
    if not (t.get("entry_ts") and t.get("exit_ts")):
        return None
    s = (datetime.fromisoformat(t["exit_ts"]) - datetime.fromisoformat(t["entry_ts"])).total_seconds()
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d}일 {h}시간" if d else f"{h}시간 {m}분" if h else f"{m}분"


def info_block(stats: dict | None = None, brief: dict | None = None, reason: str | None = None) -> list[str]:
    """알림에 붙는 종목 정보. 휴대폰에서 한눈에 읽히게 **최대 3줄**: 위험 → 오늘 성과 → 왜 샀는지."""
    s, b, L = stats or {}, brief or {}, []
    sh, bo = b.get("short") or {}, b.get("borrow")
    risk = []
    f = b.get("filings") or {}
    if f.get("dilution_score") is not None:
        risk.append(f"희석 {f['dilution_score']:.0f}")
    if sh.get("ratio") is not None:
        risk.append(f"공매도 {sh['ratio'] * 100:.0f}%")
    if bo:
        risk.append(f"대차 {bo['fee_rate']:.0f}%")
    if b.get("ssr"):
        risk.append("SSR")
    if b.get("reg_sho"):
        risk.append("RegSHO")
    if risk:
        L.append(" · ".join(risk))
    perf = []
    if s.get("change_pct") is not None:
        perf.append(pct(s["change_pct"]))
    if s.get("dollar"):
        perf.append(money_s(s["dollar"]))
    if s.get("vol_ratio"):
        perf.append(f"평소 {s['vol_ratio']:.1f}배")
    if s.get("ret_30") is not None:
        perf.append(f"30일 {pct(s['ret_30'])}")
    if perf:
        L.append("📊 " + " · ".join(perf[:4]))
    ev = next((e for e in b.get("events") or [] if e.get("summary") or e.get("title")), None)
    tail = reason or (("📰 " + (ev.get("summary") or ev.get("title") or "")[:70]) if ev else None)
    if b.get("halts"):
        h = b["halts"][0]
        tail = (tail + " · " if tail else "") + f"⏸ {h['halt_ts'][5:10]} {h['reason_ko']}"
    if tail:
        L.append(tail[:110])
    return L


def buy_filled(t: dict, qty: int, price: float, session: str, info: list[str] | None = None, name: str | None = None,
               filled: int | None = None, planned: int | None = None) -> str:
    """매수 체결 알림. filled/planned 를 주면 부분 체결과 완료를 구분해 보여준다 (토스·모의 모두 최우선 잔량까지만 체결)."""
    total = filled if filled is not None else qty
    planned = planned if planned is not None else total
    head = f"🟢 매수 {'중' if total < planned else '완료'} {sym_s(t['symbol'], name)} · {total}/{planned}주 · {px(price)}" \
        if total < planned else \
        f"🟢 매수 완료 {sym_s(t['symbol'], name)} · {total}주 · {px(price)}"
    lines = [f"{head} · {_strat(t)}/{_sess(session)}", f"총 {usd(total * price)}"]
    if t.get("stop") or t.get("target"):
        tp = (t["target"] / price - 1) * 100 if t.get("target") and price else None
        sl = (t["stop"] / price - 1) * 100 if t.get("stop") and price else None
        lines[1] += f" · 익절 {px(t.get('target'))} ({pct(tp)}) · 손절 {px(t.get('stop'))} ({pct(sl)})"
    return "\n".join(lines + (info or []))


def scaled_out(t: dict, qty: int, price: float, info: list[str] | None = None, name: str | None = None) -> str:
    gain = (price / t["entry_price"] - 1) * 100 if t.get("entry_price") else None
    return "\n".join([
        f"🟡 분할 익절 · {sym_s(t['symbol'], name)}",
        f"{qty}주 × {px(price)} ({pct(gain)})",
        f"나머지 {t['remaining']}주: 손절을 본전 {px(t.get('entry_price'))}로, 고점 대비 −{t.get('trail_pct') or 0:g}% 트레일링",
    ] + (info or []))


def closed(t: dict, info: list[str] | None = None, name: str | None = None) -> str:
    pnl = t.get("pnl") or 0.0
    icon = "🔵" if pnl >= 0 else "🔴"
    held = _held(t)
    peak = (t["high_water"] / t["entry_price"] - 1) * 100 if t.get("high_water") and t.get("entry_price") else None
    lines = [f"{icon} 청산 {sym_s(t['symbol'], name)} · {t.get('exit_reason') or '-'}",
             f"{usd(pnl, sign=True)} ({pct(t.get('pnl_pct'))}) · {t.get('exited_qty') or t.get('qty') or 0}주"
             + (f" · {held} 보유" if held else ""),
             f"{px(t.get('entry_price'))} → {px(t.get('exit_price'))}"
             + (f" · 최고 {pct(peak)}" if peak is not None else "")
             + f" · {_strat(t)}/{_sess(t.get('entry_session'))}→{_sess(t.get('exit_session'))}"]
    return "\n".join(lines + (info or []))


def backstop_fired(t: dict, name: str | None = None) -> str:
    return "\n".join([f"🛑 서버 손절 안전망 발동 · {sym_s(t['symbol'], name)}",
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
