"""데이마켓 끝물에 '전일 거래량 폭발' 신호가 있으면 사서 프리마켓 시작가에 판다.

session_pop(터짐 후 눌림)은 정규장·프리마켓 "안에서" 사고파는 전략이라 신호 구조가 다르다 — 데이마켓은
거래 자체가 얇아 같은 방식(세션 안 터짐·눌림)을 적용할 표본이 안 나왔다(signal_pop 주석 참고). 대신
2026-11-01 편향 없는 재검증(bars_1m 전체 1,758종목, 날짜순 2/3 학습·1/3 홀드아웃)으로 확인한 **다른** 신호:

  '데이마켓 끝 매수 → 프리마켓 시작 매도'는 전일 거래량이 20일 평균(중앙값)의 상위 25%(연구 표본에서는
  대략 7배 이상)인 종목만 골랐을 때 학습 평균 +1.1%, 검증 평균 +1.0%(승률 43~45%)로 train/test 가
  어긋나지 않고 일관됨. 같은 방식으로 본 애프터마켓 핸드오프는 조건을 걸수록 검증에서 마이너스로
  뒤집혀 보류했지만, 데이마켓 쪽은 유지됐다. 승률이 50% 밑이라 소수의 큰 건이 평균을 끌어올리는
  구조이고, 스프레드·수수료를 빼면 얇아질 수 있어 예산을 작게 잡는다.

precursor.daily() 가 매일 밤(장 마감 후) 전 종목에 대해 계산해 두는 vol_ratio(= 전일 거래량 /
20일 중앙값 거래량)를 그대로 쓴다 — 따로 계산하지 않고 precursor_daily 테이블을 조회.
목표·손절은 백테스트에서 안 본 값이라(그날 결과로만 검증) 넓게 잡아 대부분 예정된 시각(프리마켓
시작)에 팔리게 하고, 혹시 모를 급락에만 손절이 걸리게 한다.
"""
from __future__ import annotations

import json
from datetime import timedelta

from ..sessions import DAY, PRE
from .strategies import Ctx, Plan, Strategy, quote_check


class DayHandoff(Strategy):
    name = "day_handoff"
    SESSIONS = {DAY}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        day_bound = ctx.bound(DAY)
        pre_bound = ctx.bound(PRE)
        if not day_bound or not pre_bound:
            return []
        _, end = day_bound
        if ctx.now < end - timedelta(minutes=p["late_window_minutes"]):
            return []   # 데이마켓 끝물에만 본다 — 그래야 백테스트가 가정한 '세션 끝 가격'에 가깝게 산다
        rows = ctx.mv.q(
            "SELECT symbol, metrics FROM precursor_daily WHERE d0 = "
            "(SELECT MAX(d0) FROM precursor_daily WHERE d0 < ?)", (ctx.trade_date.isoformat(),))
        out = []
        for sym, metrics_json in rows:
            if sym in ctx.held:
                continue
            try:
                m = json.loads(metrics_json or "{}")
            except ValueError:
                continue
            vr = m.get("vol_ratio")
            if vr is None or vr < p["min_vol_ratio"]:
                continue
            if (m.get("dollar20") or 0) < p["min_dollar"]:
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"전일 거래량 {vr:.1f}배, {bad}", 0))
                continue
            price = self._entry_price(b)
            sell_from = pre_bound[0]
            out.append(self._plan(ctx, sym, price,
                                  f"데이마켓 끝물 · 전일 거래량 평소(20일)의 {vr:.1f}배 → 프리마켓 시작가에 매도",
                                  score=vr, sell_from=sell_from, context={"vol_ratio": vr}))
        return out
