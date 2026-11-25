"""알림 한 건의 결과를 같은 기준으로 잰다. 진입 = 알림 시각 '다음' 1분봉의 시가 (알림을 받고 주문하는 데 걸리는 시간 반영).
모든 알림에 같은 길이의 창(30분·2시간·6시간)을 쓴다 — 이른 알림일수록 남은 시간이 길어 유리해 보이는 착시를 막는다.
한 봉 안에서 익절선·손절선에 둘 다 닿았으면 손실로 친다(보수적). 비용은 한쪽당 cost_pct (페니주 스프레드·슬리피지)."""
from __future__ import annotations

from dataclasses import dataclass

HORIZONS = (30, 120, 360)      # 분
RACES = {"r10_5": (0.10, 0.05), "r20_10": (0.20, 0.10)}      # (익절, 손절)


@dataclass(frozen=True)
class Bar:
    ts: float      # 봉 시작 epoch 초
    o: float
    h: float
    l: float
    c: float


def race(bars: list[Bar], entry: float, up: float, down: float) -> str:
    """익절선(+up)과 손절선(−down) 중 먼저 닿은 쪽: 'win' | 'loss' | 'none'. 같은 봉에서 둘 다면 loss."""
    hi, lo = entry * (1 + up), entry * (1 - down)
    for b in bars:
        hit_lo, hit_hi = b.l <= lo, b.h >= hi
        if hit_lo:
            return "loss"
        if hit_hi:
            return "win"
    return "none"


def outcome(bars: list[Bar], alert_ts: float, cost_pct: float = 1.0) -> dict | None:
    """bars: 한 종목의 1분봉 (시간순). → 지표 dict, 진입할 봉이 없거나 가격이 0 이면 None."""
    after = [b for b in bars if b.ts > alert_ts]
    if not after or after[0].o <= 0:
        return None
    entry, t0 = after[0].o, after[0].ts
    out = {"entry": entry, "entry_delay_s": t0 - alert_ts}
    for m in HORIZONS:
        win = [b for b in after if b.ts < t0 + m * 60]
        if win[-1].ts < t0 + (m - 5) * 60:           # 창 끝 가까이까지 데이터가 없으면(거래 정지·수집 끊김) 미확인
            out[f"ret{m}"] = out[f"mfe{m}"] = out[f"mae{m}"] = None
            continue
        out[f"ret{m}"] = (win[-1].c / entry - 1) * 100 - 2 * cost_pct         # 비용 뺀 순수익률 %
        out[f"mfe{m}"] = (max(b.h for b in win) / entry - 1) * 100
        out[f"mae{m}"] = (min(b.l for b in win) / entry - 1) * 100
    w120 = [b for b in after if b.ts < t0 + 120 * 60]
    for name, (u, d) in RACES.items():
        out[name] = race(w120, entry, u, d)
    out["hit20_360"] = out["mfe360"] is not None and out["mfe360"] >= 20
    return out
