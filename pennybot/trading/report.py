"""일일 리포트: 한 영업일(데이마켓 → 정규장)이 끝나면 결과를 정리해 알림으로 보내고 파일로 남긴다.

담는 것: 그날 매매(진입·청산 세션, 순손익, 사유), 전략별 성적, 7일·30일·전체 누적,
계좌 평가액 변화, 남은 보유, 전략 후보 수와 주요 탈락 사유, 이상 징후(잔고 불일치·미청산).
파일: state/reports/<영업일>.txt (+ .json), 대시보드 '일일 리포트'에서 볼 수 있음
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SESS = {"DAY": "데이", "PRE": "프리", "REGULAR": "정규", "AFTER": "애프터"}
STRAT = {"runner_overnight": "러너 오버나이트", "dilution_catalyst": "희석 해소 촉매",
         "day_ignition": "데이마켓 점화", "day_dip": "급락 반등"}


def _utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _sum(rows: list[dict]) -> dict:
    n = len(rows)
    net = sum(r["pnl"] or 0 for r in rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    return {"trades": n, "wins": wins, "win_rate": wins / n if n else 0.0, "net": net,
            "fees": sum(r["fees"] or 0 for r in rows)}


def build(journal, trade_date: str, now: datetime, mismatch: dict | None = None) -> dict:
    mode = journal.mode
    closed = journal.q("""SELECT * FROM trades WHERE status='CLOSED' AND trade_date=? AND mode=? ORDER BY exit_ts""",
                       (trade_date, mode))
    canceled = journal.q("SELECT COUNT(*) AS n FROM trades WHERE status='CANCELED' AND trade_date=? AND mode=?",
                         (trade_date, mode))[0]["n"]
    by_strat = {}
    for name in sorted({r["strategy"] for r in closed}):
        by_strat[name] = _sum([r for r in closed if r["strategy"] == name])
    d = date.fromisoformat(trade_date)

    def since(days: int) -> dict:
        return _sum(journal.q("SELECT pnl, fees FROM trades WHERE status='CLOSED' AND mode=? AND trade_date > ? "
                              "AND trade_date <= ?", (mode, (d - timedelta(days=days)).isoformat(), trade_date)))
    total = _sum(journal.q("SELECT pnl, fees FROM trades WHERE status='CLOSED' AND mode=? AND trade_date <= ?",
                           (mode, trade_date)))
    eq = journal.q("SELECT ts, equity FROM equity WHERE mode=? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                   (mode, _utc(now)))
    eq_prev = journal.q("SELECT ts, equity FROM equity WHERE mode=? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                        (mode, _utc(now - timedelta(hours=24))))
    sig = journal.q("SELECT ok, reason, symbol FROM signals WHERE trade_date=?", (trade_date,))
    picked = {s["symbol"] for s in sig if s["ok"]}
    reasons = Counter(_short(s["reason"]) for s in sig if not s["ok"])
    left = journal.open_trades()
    best = max(closed, key=lambda r: r["pnl"] or 0, default=None)
    worst = min(closed, key=lambda r: r["pnl"] or 0, default=None)
    return {"trade_date": trade_date, "mode": mode, "created": _utc(now), "day": _sum(closed),
            "trades": [{k: r[k] for k in ("symbol", "strategy", "qty", "entry_price", "exit_price", "pnl", "pnl_pct",
                                          "fees", "exit_reason", "entry_session", "exit_session")} for r in closed],
            "canceled_entries": canceled, "by_strategy": by_strat,
            "week": since(7), "month": since(30), "total": total,
            "equity": eq[0]["equity"] if eq else None,
            "equity_change": (eq[0]["equity"] - eq_prev[0]["equity"]) if eq and eq_prev else None,
            "candidates": len(picked), "scanned": len({s["symbol"] for s in sig}),
            "top_rejections": reasons.most_common(5),
            "open": [{"symbol": t["symbol"], "remaining": t["remaining"], "status": t["status"]} for t in left],
            "mismatch": mismatch or {},
            "best": best and {"symbol": best["symbol"], "pnl": best["pnl"], "pnl_pct": best["pnl_pct"]},
            "worst": worst and {"symbol": worst["symbol"], "pnl": worst["pnl"], "pnl_pct": worst["pnl_pct"]}}


def _short(reason: str | None) -> str:
    """탈락 사유를 묶기 좋게: 숫자·종목별 세부는 떼어낸다."""
    r = (reason or "").split(" (")[0].split(":")[0]
    return "".join(ch for ch in r if not ch.isdigit() and ch not in "+-.%$").strip() or "기타"


def _usd(v: float | None, sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{'+' if sign and v > 0 else '−' if v < 0 else ''}${abs(v):,.2f}"


def _line(s: dict) -> str:
    if not s["trades"]:
        return "매매 없음"
    return f"{s['trades']}건 · 승률 {s['win_rate'] * 100:.0f}% · 순손익 {_usd(s['net'])}"


def render(rep: dict) -> str:
    """알림용 텍스트 (텔레그램·디스코드). 파일에도 같은 내용."""
    mode = "모의" if rep["mode"] == "paper" else "실거래" if rep["mode"] == "live" else rep["mode"]
    L = [f"📊 일일 리포트 {rep['trade_date']} ({mode})",
         f"오늘: {_line(rep['day'])}" + (f" (수수료 {_usd(rep['day']['fees'], False)} 차감)" if rep["day"]["fees"] else "")]
    if rep["equity"] is not None:
        L.append(f"계좌 평가액 {_usd(rep['equity'], False)}"
                 + (f" (24시간 {_usd(rep['equity_change'])})" if rep["equity_change"] is not None else ""))
    if rep["trades"]:
        L.append("")
        for t in rep["trades"]:
            L.append(f"• {t['symbol']} {STRAT.get(t['strategy'], t['strategy'])} {t['qty']}주 "
                     f"${t['entry_price']:.4f}→${t['exit_price']:.4f} {t['pnl_pct']:+.1f}% {_usd(t['pnl'])}"
                     f" [{SESS.get(t['entry_session'], '-')}→{SESS.get(t['exit_session'], '-')}] {t['exit_reason']}")
    if len(rep["by_strategy"]) > 1:
        L.append("")
        L += [f"{STRAT.get(k, k)}: {_line(v)}" for k, v in rep["by_strategy"].items()]
    L += ["", f"최근 7일: {_line(rep['week'])}", f"최근 30일: {_line(rep['month'])}", f"전체: {_line(rep['total'])}"]
    L.append("")
    L.append(f"후보 {rep['candidates']}개 / 검토 {rep['scanned']}개" +
             (f", 매수 미체결 {rep['canceled_entries']}건" if rep["canceled_entries"] else ""))
    if rep["top_rejections"]:
        L.append("주요 탈락: " + ", ".join(f"{r} {n}" for r, n in rep["top_rejections"][:3]))
    warn = []
    if rep["open"]:
        warn.append("아직 보유 " + ", ".join(f"{o['symbol']} {o['remaining']}주" for o in rep["open"]))
    if rep["mismatch"]:
        warn.append("잔고 불일치 " + ", ".join(f"{s} 봇 {v['bot']}/계좌 {v['account']}" for s, v in rep["mismatch"].items()))
    if warn:
        L += [""] + [f"⚠️ {w}" for w in warn]
    return "\n".join(L)


def save(rep: dict, text: str, reports_dir: str | Path) -> Path:
    import json
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rep['trade_date']}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str),
                                                  encoding="utf-8")
    p = d / f"{rep['trade_date']}.txt"
    p.write_text(text, encoding="utf-8")
    return p
