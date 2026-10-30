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
    """알림에 붙는 종목 정보. 한 줄 = 한 주제로 나눠서, 필요한 건 다 보이게 (휴대폰에서도 라벨만 보고 읽힌다).
    stats: MarketView.day_stats, brief: brief.brief(+with_filings)."""
    s, b, L = stats or {}, brief or {}, []
    if s:
        part = [x for x in (
            pct(s["change_pct"]) if s.get("change_pct") is not None else None,
            f"{qty_s(s.get('volume'))}주" if s.get("volume") else None,
            money_s(s.get("dollar")) if s.get("dollar") else None,
            f"평소 {s['vol_ratio']:.1f}배" if s.get("vol_ratio") else None) if x]
        if part:
            L.append("📊 오늘 " + " · ".join(part))
        chart = [x for x in (
            f"30일 {pct(s['ret_30'])}" if s.get("ret_30") is not None else None,
            f"60일 고점 대비 {pct(s['dd_60'])}" if s.get("dd_60") is not None else None,
            f"변동폭 {s['range_30']:.0f}%" if s.get("range_30") is not None else None) if x]
        if chart:
            L.append("📉 " + " · ".join(chart))
    sh, bo = b.get("short") or {}, b.get("borrow")
    part = []
    if sh.get("ratio") is not None:
        part.append(f"공매도 {sh['ratio'] * 100:.0f}%")
    if bo:
        part.append(f"대차 {bo['fee_rate']:.0f}%" + (f" ({qty_s(bo['available'])}주)" if bo.get("available") else ""))
    if b.get("ssr"):
        part.append("SSR")
    if b.get("reg_sho"):
        part.append("RegSHO")
    if part:
        L.append("🩳 " + " · ".join(part))
    f = b.get("filings") or {}
    part = []
    if f.get("dilution_score") is not None:
        part.append(f"희석 {f['dilution_score']:.0f}점")
    if f.get("motives"):
        part.append("동기: " + ", ".join(m["label"] for m in f["motives"][:3]))
    if part:
        L.append("💧 " + " · ".join(part))
    evs = [e for e in b.get("events") or [] if e.get("summary") or e.get("title")][:2]
    for ev in evs:
        L.append(f"📰 {ev['ts'][5:10]} " + (ev.get("summary") or ev.get("title") or "")[:120])
    if b.get("halts"):
        h = b["halts"][0]
        L.append(f"⏸ 정지 {h['halt_ts'][5:16].replace('T', ' ')} ET {h['reason_ko']}")
    if reason:
        L.append(f"💬 {reason[:140]}")
    return ([""] + L) if L else []


def buy_filled(t: dict, qty: int, price: float, session: str, info: list[str] | None = None, name: str | None = None,
               filled: int | None = None, planned: int | None = None) -> str:
    """매수 체결 알림. 모든 알림 공통: 아이콘·사건·종목 / 숫자 / 맥락 순."""
    total = filled if filled is not None else qty
    planned = planned if planned is not None else total
    head = f"🟢 매수 {'중' if total < planned else '완료'} · {sym_s(t['symbol'], name)}"
    if total < planned:
        head += f" · {total}/{planned}주"
    lines = [head, f"💵 {px(price)} · {total}주 · 총 {usd(total * price)}"]
    bits = []
    if t.get("target") and price:
        bits.append(f"🎯 익절 {px(t['target'])} ({pct((t['target'] / price - 1) * 100)})")
    if t.get("stop") and price:
        bits.append(f"🛑 손절 {px(t['stop'])} ({pct((t['stop'] / price - 1) * 100)})")
    bits.append(f"{_strat(t)}/{_sess(session)}")
    lines.append(" · ".join(bits))
    return "\n".join(lines + (info or []))


def scaled_out(t: dict, qty: int, price: float, info: list[str] | None = None, name: str | None = None) -> str:
    gain = (price / t["entry_price"] - 1) * 100 if t.get("entry_price") else None
    lines = [f"🟡 분할 익절 · {sym_s(t['symbol'], name)}",
             f"{pct(gain)} · {qty}주 매도 · 잔여 {t['remaining']}주",
             "",
             "💰 결과",
             f"{qty}주 × {px(price)} ({pct(gain)})",
             "",
             "🛡 손절",
             f"손절선 본전 {px(t.get('entry_price'))}로 이동",
             f"트레일링 고점 대비 −{t.get('trail_pct') or 0:g}%"]
    return "\n".join(lines + (info or []))


def closed(t: dict, info: list[str] | None = None, name: str | None = None) -> str:
    pnl = t.get("pnl") or 0.0
    win = pnl >= 0
    icon = "🔵" if win else "🔴"
    held = _held(t)
    peak = (t["high_water"] / t["entry_price"] - 1) * 100 if t.get("high_water") and t.get("entry_price") else None
    qty = t.get("exited_qty") or t.get("qty") or 0
    head = f"{icon} {sym_s(t['symbol'], name)} 청산"
    lines = [head,
             f"{usd(pnl, sign=True)} ({pct(t.get('pnl_pct'))}) · {qty}주" + (f" · {held} 보유" if held else ""),
             "", "💰 결과",
             f"가격 {px(t.get('entry_price'))} → {px(t.get('exit_price'))}" + (f" (최고 {pct(peak)})" if peak is not None else ""),
             f"사유 {t.get('exit_reason') or '-'}"]
    lines += ["", "🧭 진입", f"{_strat(t)} · {_sess(t.get('entry_session'))} → {_sess(t.get('exit_session'))}"]
    if t.get("reason"):
        lines.append(f"📋 매수 이유: {t['reason'][:80]}")
    return "\n".join(lines + (info or []))


def backstop_fired(t: dict, name: str | None = None) -> str:
    return "\n".join([f"🛑 서버 손절 안전망 · {sym_s(t['symbol'], name)}",
                      f"💵 남은 {t.get('remaining') or 0}주 · 발동가 {px(t.get('backstop_trigger'))}",
                      "→ 토스 서버가 손절 주문을 냈습니다. 봇이 이어서 체결을 추적합니다"])


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

def signal(strategy: str, symbol: str, price: float, reason: str, session: str, score: float | None = None,
           name: str | None = None) -> str:
    """수급 신호 알림 — 주문 직전에 '지금 이 종목이 잡혔다'를 실시간으로 보낸다."""
    lines = [f"⚡ 수급 신호 · {sym_s(symbol, name)}",
             f"💵 {px(price)} · {_sess(session)} · {_strat({'strategy': strategy})}"]
    if reason:
        lines.append(f"💬 {reason[:140]}")
    if score is not None:
        lines.append(f"점수 {score:.0f}")
    return "\n".join(lines)
