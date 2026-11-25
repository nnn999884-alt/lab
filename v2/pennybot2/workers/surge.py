"""비교용 '늦은 감지'(5분 안 +10%)와, 전날 후보 정보를 신호에 붙이는 enrich. 실시간 프로세스는 없다 — 백테스트(backtest-ignite)가 조기 감지와 비교하는 기준으로,
ignite 가 enrich 를 쓴다. (이미 오른 뒤의 신호라 알림에는 쓰지 않는다 — LESSONS A-3)"""
from __future__ import annotations

from collections import defaultdict, deque



class SurgeDetector:
    def __init__(self, pct: float = 10.0, window_sec: int = 300, cooldown_sec: int = 900,
                 min_price: float = 0.0, max_price: float = 1e9):
        self.pct, self.window, self.cooldown = pct, window_sec, cooldown_sec
        self.min_price, self.max_price = min_price, max_price
        self._hist: dict[str, deque] = defaultdict(deque)
        self._last: dict[str, float] = {}

    def feed(self, symbol: str, ts: float, price: float) -> dict | None:
        if price <= 0 or not (self.min_price <= price <= self.max_price):
            return None
        h = self._hist[symbol]
        h.append((ts, price))
        while h and ts - h[0][0] > self.window:
            h.popleft()
        gain = (price / min(p for _, p in h) - 1) * 100
        if gain < self.pct or ts - self._last.get(symbol, -1e18) < self.cooldown:
            return None
        self._last[symbol] = ts
        return {"s": symbol, "ts": ts, "kind": "surge", "value": gain, "price": price,
                "text": f"{symbol} {self.window // 60}분 안에 +{gain:.1f}% (${price:.4f})\n"
                        f"⚠️ 이미 오른 뒤 — 추격 주의 (이런 알림에 따라 사면 과거 −5% 먼저 61%)"}


def enrich(sig: dict, cand: dict | None) -> dict:
    """전날 밤 후보였던 종목이면 신호에 점수와 이유를 붙인다."""
    if not cand:
        return sig
    sig = dict(sig)
    if cand.get("prob") is not None:       # 시험 구간에서 채택된 모델의 확률 (교정 확인됨)
        sig["text"] += f"\n★ 어젯밤 모델 후보: 오늘 급등 확률 약 {cand['prob'] * 100:.1f}% — " + ", ".join(cand["why"][:3])
    else:                                  # 옛 규칙 점수 (확률 미검증)
        sig["text"] += f"\n★ 어젯밤 후보 {cand['score']}점 (확률은 재검증 중) — " + ", ".join(cand["why"][:4])
    sig["candidate_score"] = cand["score"]
    return sig
