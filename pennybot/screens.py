"""해외 스크리너 조건 — 해외 소형주 트레이더·스캐너 업체가 공개한 조건을 조건검색 식으로 옮긴 것.

출처(2026-11-08 조사): Ross Cameron '5대 조건'(TradingView 정리 글), Tapeboard 스캐너 프리셋 56개 중 소형주·롱 관련,
Trade Ideas 스캔 예시, r/pennystocks Finviz 설정, TradingView 공개 스크립트(NR7·거래량 마름).
공매도 전용 식(하락 종목·고점 붕괴 등)과 대형주·재무 식은 뺐다 (토스는 공매도 불가, 페니주와 무관).

- condscan 의 식으로 들어가 대시보드 '조건검색' 화면에서 고를 수 있다 (그룹 '해외').
- 조건은 지표(feature) 범위 하나씩: {"t": "f", "k": "chg", "min": 10}. 세션 조건은 {"t": "ses", "in": ["PRE"]}.
- `pennybot screen-eval` 은 과거 1분봉의 터짐 순간마다 모든 해외 식을 걸어 적중률·순수익을 비교한다 (읽기 전용).
  2026-11-08 첫 검증: 적중률(+30% 도달)은 조건 없음의 10배 이상이지만 따라 사면 전부 손해 → 알림 선별용이지 매수 규칙이 아님.

데이터 한계: 유통주식은 발행주식(stock_info.shares)으로 대신한다. 공시·뉴스 기록은 10/21 께부터라 그 전 증자는 모른다.
공매도 잔고 자료가 없어 '공매도 많은 종목'은 FINRA 공매도 거래 비중과 IBKR 대차 수수료로 대신한다.
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SHELF_FORMS = {"S-3", "S-3/A", "S-3ASR", "F-3", "F-3/A", "S-1", "S-1/A", "S-1MEF", "F-1", "F-1/A",
               "424B3", "424B4", "424B5", "424B7", "EFFECT"}

# 지표: 키 → (설명, 단위)
FEATURES: dict[str, tuple[str, str]] = {
    "px": ("가격", "$"), "chg": ("전일 종가 대비", "%"), "rvol": ("상대거래량(오늘÷20일 중앙값)", "배"),
    "dayvol": ("오늘 거래량", "주"), "dollar": ("오늘 거래대금", "$"), "float": ("발행주식(유통주식 대신)", "주"),
    "rot": ("회전율(오늘 거래량÷발행주식)", "배"), "mcap": ("시가총액", "$"), "hod_dist": ("오늘 고점까지", "%"),
    "vwap_dist": ("VWAP 대비", "%"), "vwap_reclaim": ("15분 전엔 VWAP 아래, 지금 위", ""),
    "vol5x": ("최근 5분 거래량÷직전 1시간 5분 평균", "배"), "r5": ("5분 전 대비", "%"),
    "orb": ("정규장 첫 15분 고점 돌파", ""), "red_green": ("전일 종가 −1% 아래 갔다가 +0.5% 위로", ""),
    "reversal": ("전일 대비 −5% 저점에서 +3% 반등", ""), "hi52_dist": ("52주 고점까지", "%"),
    "lo52_dist": ("52주 저점 위", "%"), "rsi14": ("일봉 RSI(14)", ""), "runup": ("최근 120일 하루 최대 상승", "%"),
    "nr7": ("전날 변동폭이 7일 중 최저", ""), "dry": ("최근 5일 거래량이 20일 중앙값의 절반 미만", ""),
    "spread": ("호가 스프레드", "%"), "dil180": ("180일 안 악재(증자) 공시", "건"),
    "shelf": ("180일 안 증자 등록·공모 서류", "건"), "news24": ("24시간 안 보도자료", "건"),
    "fee": ("대차 수수료(연)", "%"), "short_ratio": ("전일 공매도 거래 비중", "%"),
    "resume_min": ("거래정지 재개 후", "분"), "et_min": ("미국 동부 시각(0시부터 분)", "분"),
}


def _f(k, lo=None, hi=None) -> dict:
    c = {"t": "f", "k": k}
    if lo is not None:
        c["min"] = lo
    if hi is not None:
        c["max"] = hi
    return c


def _ses(*s) -> dict:
    return {"t": "ses", "in": list(s)}


M = 1e6
# 이름은 20자 안쪽 (대시보드 주소에 실림). desc 에 출처 조건을 그대로 적는다.
_DEFS: list[tuple[str, str, list[dict]]] = [
    ("로스 5대 조건", "Ross Cameron: $1~20 · +10% · 상대거래량 5배 · 유통 2천만 주 이하",
     [_f("px", 1, 20), _f("chg", 10), _f("rvol", 5), _f("float", hi=20 * M)]),
    ("로스 A급", "Ross Cameron A급: $5~10 · 유통 1천만 이하 · 상대거래량 10배 · +25% · 회전율 1배 · 스프레드 1% 이하",
     [_f("px", 5, 10), _f("float", hi=10 * M), _f("rvol", 10), _f("chg", 25), _f("rot", 1), _f("spread", hi=1)]),
    ("프리 러너", "Tapeboard Pre-Market Runners: 프리마켓 · $1~20 · 유통 2천만 미만 · +4% · 상대거래량 5배",
     [_ses("PRE"), _f("px", 1, 20), _f("float", hi=20 * M), _f("chg", 4), _f("rvol", 5)]),
    ("프리 갭상승", "Tapeboard Pre-Market Gap Up: 프리마켓 · +3% · 거래량 5만 주 이상",
     [_ses("PRE"), _f("chg", 3), _f("dayvol", 50_000)]),
    ("갭앤고", "Tapeboard Gap & Go: 프리마켓 · 유통 5천만 미만 · +5% · 상대거래량 3배",
     [_ses("PRE"), _f("float", hi=50 * M), _f("chg", 5), _f("rvol", 3)]),
    ("지금 모멘텀", "Tapeboard Momentum Now: $1~20 · 거래량 10만 주 이상 · 5분 +3%",
     [_f("px", 1, 20), _f("dayvol", 100_000), _f("r5", 3)]),
    ("상승률 상위", "Tapeboard Top Gainers: +2% · 거래량 50만 주 이상", [_f("chg", 2), _f("dayvol", 500_000)]),
    ("고점 모멘텀", "Tapeboard HOD Momentum: +1% · 고점 5% 이내 · 상대거래량 1.3배",
     [_f("chg", 1), _f("hod_dist", hi=5), _f("rvol", 1.3)]),
    ("오전 모멘텀", "Tapeboard Morning Momentum: 정규장 10:30 ET 전 · +2% · 상대거래량 1.5배 · 거래량 30만 주",
     [_ses("REGULAR"), _f("et_min", hi=630), _f("chg", 2), _f("rvol", 1.5), _f("dayvol", 300_000)]),
    ("치솟는 중", "Tapeboard Running Up: +3% · 고점 2% 이내 · 상대거래량 2배",
     [_f("chg", 3), _f("hod_dist", hi=2), _f("rvol", 2)]),
    ("파워아워 급증", "Tapeboard Power Hour Surge: 정규장 15시 ET 이후 · $2 이상 · +2% · 5분 거래량 3배",
     [_ses("REGULAR"), _f("et_min", 900), _f("px", 2), _f("chg", 2), _f("vol5x", 3)]),
    ("회전율 0.5배", "Tapeboard Float Rotation: 오늘 거래량이 유통주식의 50% 이상", [_f("rot", 0.5)]),
    ("회전율 1배", "Ross Cameron 우선순위: 오늘 거래량이 유통주식 이상", [_f("rot", 1)]),
    ("깨끗한 러너", "Tapeboard Clean Runners: +4% · 상대거래량 2배 · 180일 안 증자 서류·악재 공시 없음",
     [_f("chg", 4), _f("rvol", 2), _f("dil180", hi=0), _f("shelf", hi=0)]),
    ("희석 지뢰(주의)", "Tapeboard Dilution Landmines: +8% · 상대거래량 3배 · 증자 등록·공모 서류 있음 — 피할 종목",
     [_f("chg", 8), _f("rvol", 3), _f("shelf", 1)]),
    ("이상 거래량", "Tapeboard Unusual Volume: 상대거래량 2배 · 거래량 50만 주", [_f("rvol", 2), _f("dayvol", 500_000)]),
    ("5분 거래량 급증", "Tapeboard Volume Spike: 최근 5분 거래량이 평소 3배", [_f("vol5x", 3)]),
    ("상대거래량 3배", "Tapeboard Relative Volume: 상대거래량 3배 · 거래량 20만 주", [_f("rvol", 3), _f("dayvol", 200_000)]),
    ("52주 고점 돌파", "Tapeboard High Volume Breakout: +1% · 52주 고점 10% 이내 · 상대거래량 1.5배",
     [_f("chg", 1), _f("hi52_dist", hi=10), _f("rvol", 1.5)]),
    ("52주 고점 근처", "Tapeboard Near 52-Week High: 52주 고점 5% 이내", [_f("hi52_dist", hi=5)]),
    ("52주 저점 근처", "Tapeboard Near 52-Week Low: 52주 저점 5% 이내", [_f("lo52_dist", hi=5)]),
    ("RSI 과매도", "Tapeboard Oversold: 일봉 RSI(14) 30 미만", [_f("rsi14", hi=30)]),
    ("RSI 과매수", "Tapeboard Overbought: 일봉 RSI(14) 70 초과", [_f("rsi14", 70)]),
    ("VWAP 위", "Tapeboard Above VWAP: +0.5% · VWAP 위", [_f("chg", 0.5), _f("vwap_dist", 0)]),
    ("VWAP 회복", "Tapeboard Typical-Price Reclaim: +1% · 상대거래량 1.2배 · 15분 전 VWAP 아래 → 지금 위",
     [_f("chg", 1), _f("rvol", 1.2), _f("vwap_reclaim", 1)]),
    ("반등 후보", "Tapeboard Reversal Candidates: $2 이상 · 전일 대비 −5% 저점에서 +3% 반등", [_f("px", 2), _f("reversal", 1)]),
    ("시가 범위 돌파", "Tapeboard Opening Range Breakout: +1% · 상대거래량 1.5배 · 거래량 30만 주 · 첫 15분 고점 돌파",
     [_f("chg", 1), _f("rvol", 1.5), _f("dayvol", 300_000), _f("orb", 1)]),
    ("레드투그린", "Tapeboard Red-to-Green: 상대거래량 1.5배 · 전일 종가 −1% 아래 갔다가 +0.5% 위로",
     [_f("rvol", 1.5), _f("red_green", 1)]),
    ("소형주 로켓", "Tapeboard Small Cap Rockets: +5% · 거래량 50만 주 · 시총 20억 달러 미만",
     [_f("chg", 5), _f("dayvol", 500_000), _f("mcap", hi=2e9)]),
    ("페니 거래 상위", "Tapeboard Penny Stock Movers: $1~5 · 거래량 100만 주", [_f("px", 1, 5), _f("dayvol", 1_000_000)]),
    ("1달러 미만 러너", "Tapeboard Sub-$1 Runners: $1 미만 · +5% · 거래량 50만 주",
     [_f("px", hi=0.9999), _f("chg", 5), _f("dayvol", 500_000)]),
    ("마이크로캡 모멘텀", "Tapeboard Microcap Momentum: $1~10 · 유통 1,500만 이하 · +5% · 상대거래량 3배 · 시총 3억 달러 미만",
     [_f("px", 1, 10), _f("float", hi=15 * M), _f("chg", 5), _f("rvol", 3), _f("mcap", hi=3e8)]),
    ("저유통 러너", "Tapeboard Low Float Runners: 유통 2천만 미만 · +3%", [_f("float", hi=20 * M), _f("chg", 3)]),
    ("초저유통", "DilutionTracker 500%+ 급등주 공통점: 유통 800만 주 미만 · 시총 2,500만 달러 미만 · $10 미만",
     [_f("float", hi=8 * M), _f("mcap", hi=25e6), _f("px", hi=10)]),
    ("애프터 급등", "Tapeboard After-Hours Movers: 애프터마켓 · +2%", [_ses("AFTER"), _f("chg", 2)]),
    ("뉴스 급등", "Tapeboard Breaking News Movers: +3% · 상대거래량 2배 · 24시간 안 보도자료",
     [_f("chg", 3), _f("rvol", 2), _f("news24", 1)]),
    ("대차 비쌈", "Tapeboard Expensive Borrow Watch: 대차 수수료 연 25% 이상", [_f("fee", 25)]),
    ("숏스퀴즈 후보", "Tapeboard Squeeze In-Play 대용: 대차 수수료 25% 이상 · +3% · 상대거래량 3배",
     [_f("fee", 25), _f("chg", 3), _f("rvol", 3)]),
    ("공매도 비중 높음", "Tapeboard High Short Interest 대용: $2 이상 · 전일 공매도 거래 비중 50% 이상 (공매도 잔고 자료 없음)",
     [_f("px", 2), _f("short_ratio", 50)]),
    ("과거 급등주", "Tapeboard Former Runners: +2% · 상대거래량 1.5배 · 시총 5억 달러 미만 · 최근 120일 하루 +100% 이력",
     [_f("chg", 2), _f("rvol", 1.5), _f("mcap", hi=5e8), _f("runup", 100)]),
    ("정지 재개", "Tapeboard Halt Resume: 거래정지 재개 30분 이내", [_f("resume_min", 0, 30)]),
    ("Finviz 레딧식", "r/pennystocks Finviz: $1 미만 · 상대거래량 1.5배", [_f("px", hi=0.9999), _f("rvol", 1.5)]),
    ("NR7", "TradingView: 전날 변동폭이 최근 7일 중 가장 좁음", [_f("nr7", 1)]),
    ("거래량 마름", "TradingView Volume Dry Up: 최근 5일 거래량 < 20일 중앙값의 절반", [_f("dry", 1)]),
    ("마름+NR7", "TradingView: 거래량 마름과 NR7 동시", [_f("dry", 1), _f("nr7", 1)]),
]
PRESETS: dict[str, dict] = {n: {"label": n, "desc": d, "group": "해외", "min_ok": len(c), "conds": c} for n, d, c in _DEFS}


def cond_text(c: dict) -> str | None:
    if c["t"] == "ses":
        return "세션 " + "·".join({"DAY": "데이", "PRE": "프리", "REGULAR": "정규", "AFTER": "애프터"}.get(s, s) for s in c["in"])
    if c["t"] != "f":
        return None
    name, unit = FEATURES.get(c["k"], (c["k"], ""))
    if c["k"] == "et_min":
        hm = lambda m: f"{int(m) // 60:02d}:{int(m) % 60:02d}"
        return " · ".join(([f"동부 {hm(c['min'])} 이후"] if "min" in c else []) + ([f"동부 {hm(c['max'])} 전"] if "max" in c else []))
    if unit == "" and c.get("min") == 1 and "max" not in c:
        return name

    def fmt(v):
        if unit == "$" and abs(v) >= 1e6:
            return f"${v / 1e6:,.0f}M" if v < 1e9 else f"${v / 1e9:g}B"
        if unit == "$":
            return f"${v:g}"
        if unit == "주" and v >= 1e4:
            return f"{v / 1e4:,.0f}만 주"
        return f"{v:g}{unit}"
    lo, hi = c.get("min"), c.get("max")
    if lo is not None and hi is not None:
        return f"{name} {fmt(lo)}~{fmt(hi)}"
    if lo is not None:
        return f"{name} {fmt(lo)} 이상"
    if hi == 0:
        return f"{name} 없음"
    return f"{name} {fmt(hi)} 이하"


def in_range(c: dict, v) -> bool | None:
    if v is None:
        return None
    v = float(v)
    return c.get("min", -1e18) <= v <= c.get("max", 1e18)


# ---------- 지표 계산 ----------
class Feat:
    """한 종목·한 순간의 지표. 필요한 것만 계산한다 (lazy).

    day: 오늘(같은 trade_date) 1분봉 [(ts_iso, ses, o, h, l, c, v)] — 지금 봉까지
    prior: 오늘 전 일봉 [(d, o, h, l, c, v)] (오래된 → 최근)
    side: 종목별 부가자료 {"filings": [(accepted, form, direction)], "news": [published], "halts": [(halt, resume)],
          "fee": 값, "short_ratio": 값, "spread": 값}
    """

    def __init__(self, day: list, prior: list, shares: float | None, side: dict | None = None):
        self.day, self.prior, self.sh, self.side = day, prior, shares, side or {}
        self.now = day[-1][0]
        self.ses = day[-1][1]
        self.px = day[-1][5]
        self._c: dict = {}

    def get(self, k: str):
        if k not in self._c:
            try:
                self._c[k] = getattr(self, "f_" + k)()
            except (ZeroDivisionError, ValueError, IndexError, TypeError):
                self._c[k] = None
        return self._c[k]

    def _pc(self):
        return self.prior[-1][4] if self.prior and self.prior[-1][4] else None

    def _cumv(self):
        return sum(b[6] for b in self.day)

    def f_px(self):
        return self.px

    def f_chg(self):
        pc = self._pc()
        return (self.px / pc - 1) * 100 if pc else None

    def f_rvol(self):
        vols = sorted(x[5] for x in self.prior[-20:] if x[5])
        if len(vols) < 5:
            return None
        return self._cumv() / vols[len(vols) // 2]

    def f_dayvol(self):
        return self._cumv()

    def f_dollar(self):
        return sum(b[5] * b[6] for b in self.day)

    def f_float(self):
        return self.sh

    def f_rot(self):
        return self._cumv() / self.sh if self.sh else None

    def f_mcap(self):
        return self.sh * self.px if self.sh else None

    def f_hod_dist(self):
        hod = max(b[3] for b in self.day)
        return (hod - self.px) / hod * 100

    def _vwap(self, upto: int | None = None):
        bs = self.day if upto is None else self.day[:upto]
        v = sum(b[6] for b in bs)
        return sum((b[3] + b[4] + b[5]) / 3 * b[6] for b in bs) / v if v else None

    def f_vwap_dist(self):
        w = self._vwap()
        return (self.px / w - 1) * 100 if w else None

    def f_vwap_reclaim(self):
        if len(self.day) < 20:
            return None
        w0, w1 = self._vwap(), self._vwap(len(self.day) - 15)
        return int(bool(w0 and w1 and self.day[-16][5] < w1 and self.px > w0))

    def f_vol5x(self):
        if len(self.day) < 65:
            return None
        base = sum(b[6] for b in self.day[-65:-5]) / 12
        return sum(b[6] for b in self.day[-5:]) / max(base, 1.0)

    def f_r5(self):
        return (self.px / self.day[-6][5] - 1) * 100 if len(self.day) >= 6 and self.day[-6][5] else None

    def f_orb(self):
        reg = [b for b in self.day if b[1] == "REGULAR"]
        if self.ses != "REGULAR" or len(reg) <= 15:
            return None
        return int(self.px > max(b[3] for b in reg[:15]))

    def f_red_green(self):
        pc = self._pc()
        if not pc:
            return None
        return int(min(b[4] for b in self.day) <= pc * 0.99 and self.px >= pc * 1.005)

    def f_reversal(self):
        pc = self._pc()
        if not pc:
            return None
        lo = min(b[4] for b in self.day)
        return int(lo <= pc * 0.95 and self.px >= lo * 1.03)

    def f_hi52_dist(self):
        if len(self.prior) < 60:
            return None
        hi = max([x[2] for x in self.prior[-250:]] + [max(b[3] for b in self.day)])
        return (hi - self.px) / hi * 100

    def f_lo52_dist(self):
        if len(self.prior) < 60:
            return None
        lo = min([x[3] for x in self.prior[-250:] if x[3] > 0] + [min(b[4] for b in self.day)])
        return (self.px / lo - 1) * 100

    def f_rsi14(self):
        cl = [x[4] for x in self.prior[-30:]] + [self.px]
        if len(cl) < 16:
            return None
        g = l_ = 0.0
        for a, b in zip(cl[-15:-1], cl[-14:]):
            d = b - a
            g += max(d, 0)
            l_ += max(-d, 0)
        return 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)

    def f_runup(self):
        p = self.prior[-121:]
        if len(p) < 20:
            return None
        return max((p[k][2] / p[k - 1][4] - 1) * 100 for k in range(1, len(p)) if p[k - 1][4])

    def f_nr7(self):
        if len(self.prior) < 7:
            return None
        rng = [(x[2] - x[3]) / x[4] if x[4] else 9 for x in self.prior[-7:]]
        return int(rng[-1] == min(rng))

    def f_dry(self):
        vols = sorted(x[5] for x in self.prior[-20:] if x[5])
        if len(vols) < 10:
            return None
        return int(st.mean(x[5] or 0 for x in self.prior[-5:]) < 0.5 * vols[len(vols) // 2])

    def f_spread(self):
        return self.side.get("spread")

    def _since(self, days: float) -> str:
        return (datetime.fromisoformat(self.now) - timedelta(days=days)).isoformat()

    def f_dil180(self):
        lo = self._since(180)
        return sum(1 for a, _f, d in self.side.get("filings", ()) if lo <= a <= self.now and (d or 0) < 0)

    def f_shelf(self):
        lo = self._since(180)
        return sum(1 for a, f, _d in self.side.get("filings", ()) if lo <= a <= self.now and f in SHELF_FORMS)

    def f_news24(self):
        lo = self._since(1)
        return sum(1 for p in self.side.get("news", ()) if p and lo <= p <= self.now)

    def f_fee(self):
        return self.side.get("fee")

    def f_short_ratio(self):
        return self.side.get("short_ratio")

    def f_resume_min(self):
        now = datetime.fromisoformat(self.now)
        best = None
        for _h, r in self.side.get("halts", ()):
            if not r:
                continue
            try:
                rt = datetime.fromisoformat(r)
            except ValueError:
                continue
            m = (now - rt).total_seconds() / 60
            if 0 <= m and (best is None or m < best):
                best = m
        return best

    def f_et_min(self):
        t = datetime.fromisoformat(self.now).astimezone(ET)
        return t.hour * 60 + t.minute


def evaluate(cond: dict, feat: Feat | None) -> bool | None:
    if feat is None:
        return None
    if cond["t"] == "ses":
        return feat.ses in cond["in"]
    return in_range(cond, feat.get(cond["k"]))


def uses_feat(conds: list[dict]) -> bool:
    return any(c["t"] in ("f", "ses") for c in conds)


def _iso(ts) -> str:
    """DB 시각 문자열을 비교 가능한 UTC ISO 로 (+00:00)."""
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat()


def load_side(con: sqlite3.Connection, symbols=None, *, live: bool = True) -> dict[str, dict]:
    """부가자료를 종목별로 한 번에 읽는다. live=False(과거 검증)면 대차·공매도·스프레드를 날짜별 목록으로 둔다."""
    want = set(symbols) if symbols is not None else None
    side: dict[str, dict] = defaultdict(lambda: {"filings": [], "news": [], "halts": []})

    def ok(s):
        return want is None or s in want

    def q(sql, a=()):
        try:
            return con.execute(sql, a).fetchall()
        except sqlite3.Error:
            return []
    for s, a, f, d in q("SELECT symbol, accepted, form, direction FROM filings WHERE symbol IS NOT NULL"):
        if ok(s):
            side[s]["filings"].append((_iso(a), f, d))
    for s, p in q("SELECT symbol, published FROM news WHERE symbol IS NOT NULL"):
        if ok(s):
            side[s]["news"].append(_iso(p))
    for s, h, r in q("SELECT symbol, halt_ts, resume_ts FROM halts"):
        if ok(s):
            side[s]["halts"].append((_iso(h), _iso(r) if r else None))
    if live:
        for s, fee in q("SELECT b.symbol, b.fee_rate FROM borrow b JOIN (SELECT symbol, MAX(ts) m FROM borrow GROUP BY symbol) x "
                        "ON b.symbol=x.symbol AND b.ts=x.m"):
            if ok(s):
                side[s]["fee"] = fee
        for s, sv, tv in q("SELECT v.symbol, v.short_volume, v.total_volume FROM short_volume v JOIN "
                           "(SELECT symbol, MAX(trade_date) m FROM short_volume GROUP BY symbol) x ON v.symbol=x.symbol AND v.trade_date=x.m"):
            if ok(s) and tv:
                side[s]["short_ratio"] = sv / tv * 100
    else:
        for s, d, fee in q("SELECT symbol, substr(ts,1,10), MAX(fee_rate) FROM borrow GROUP BY symbol, substr(ts,1,10)"):
            if ok(s):
                side[s].setdefault("fee_by_d", []).append((d, fee))
        for s, d, sv, tv in q("SELECT symbol, trade_date, short_volume, total_volume FROM short_volume"):
            if ok(s) and tv:
                side[s].setdefault("sr_by_d", []).append((d, sv / tv * 100))
        for s, d, sp in q("SELECT symbol, trade_date, MIN(spread_med_pct) FROM session_summary "
                          "WHERE spread_med_pct IS NOT NULL GROUP BY symbol, trade_date"):
            if ok(s):
                side[s].setdefault("sp_by_d", []).append((d, sp))
        for v in side.values():
            for k in ("fee_by_d", "sr_by_d", "sp_by_d"):
                v.get(k, []).sort()
    return side


def _before(rows: list, d: str):
    """[(날짜, 값)] 정렬 목록에서 d 보다 앞선 마지막 값 (그날 값은 미래 정보라 안 쓴다)."""
    v = None
    for x, val in rows:
        if x >= d:
            break
        v = val
    return v


def side_at(sd: dict | None, d: str) -> dict:
    """과거 검증용: trade_date d 시점에 알 수 있던 부가자료."""
    if not sd:
        return {}
    return {"filings": sd["filings"], "news": sd["news"], "halts": sd["halts"],
            "fee": _before(sd.get("fee_by_d", []), d), "short_ratio": _before(sd.get("sr_by_d", []), d),
            "spread": _before(sd.get("sp_by_d", []), d)}


def live_spread(con: sqlite3.Connection, sym: str, now_iso: str) -> float | None:
    try:
        r = con.execute("SELECT bid, ask FROM quotes WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                        (sym, now_iso)).fetchone()
    except sqlite3.Error:
        return None
    if not r or not r[0] or not r[1] or r[1] < r[0]:
        return None
    return (r[1] - r[0]) / ((r[0] + r[1]) / 2) * 100


# ---------- 과거 검증 (screen-eval) ----------
def extract(db_path: str, since: str) -> list[dict]:
    """ruleeval 과 같은 '터짐' 순간마다 모든 해외 식의 통과 여부와 이후 결과(+30%/−15%/180분)를 만든다."""
    from . import ruleeval as R
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=120)
    try:
        shares = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares>0"))
        floor = (datetime.fromisoformat(since) - timedelta(days=400)).date().isoformat()
        daily: dict[str, list] = defaultdict(list)
        for row in con.execute("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d>=? ORDER BY symbol, d", (floor,)):
            daily[row[0]].append(row[1:])
        side = load_side(con, live=False)
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM bars_1m WHERE trade_date>=? AND trade_date IS NOT NULL ORDER BY 1", (since,))]
        out: list[dict] = []
        for d in dates:
            by: dict[str, list] = defaultdict(list)
            for s, ts, ses, o, h, lo, c, v in con.execute(
                    "SELECT symbol, ts, session, open, high, low, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (d,)):
                by[s].append((ts, ses, o, h, lo, c, v or 0))
            for s, bars in by.items():
                if len(bars) < 40:
                    continue
                prior = [x for x in daily.get(s, ()) if x[0] < d]
                if len(prior) < 8:
                    continue
                out.extend(_events(s, d, bars, shares.get(s), prior, side_at(side.get(s), d), R))
        return out
    finally:
        con.close()


def _events(sym, d, bars, sh, prior, sd, R) -> list[dict]:
    seen: set = set()
    out = []
    for i in range(32, len(bars)):
        ts, ses, _o, _h, _l, c, _v = bars[i]
        if ses in seen or c <= 0 or c >= 10:
            continue
        w3 = bars[i - 2:i + 1]
        d3 = sum(b[5] * b[6] for b in w3)
        v3 = sum(b[6] for b in w3)
        base = sum(b[6] for b in bars[i - 32:i - 2]) / 30
        if d3 < R.MIN_DOLLAR_3M or v3 / 3 < R.MIN_VOL_X * max(base, 1.0):
            continue
        seen.add(ses)
        end = (datetime.fromisoformat(ts) + timedelta(minutes=R.HORIZON_MIN)).isoformat()
        fut = [b for b in bars[i + 1:] if b[0] <= end]
        if len(fut) < 5:
            continue
        res = None
        for b in fut:
            if b[4] <= c * (1 - R.SL / 100):
                res = -R.SL
                break
            if b[3] >= c * (1 + R.TP / 100):
                res = R.TP
                break
        if res is None:
            res = (fut[-1][5] / c - 1) * 100
        f = Feat(bars[:i + 1], prior, sh, sd)
        hits = [n for n, p in PRESETS.items() if all(evaluate(cd, f) for cd in p["conds"])]
        out.append({"d": d, "s": sym, "ses": ses, "hits": hits, "up": (max(b[3] for b in fut) / c - 1) * 100,
                    "ret": res})
    return out


def summarize(evs: list[dict]) -> dict:
    from . import ruleeval as R
    n = len(evs)
    if not n:
        return {"n": 0}
    k = sum(e["up"] >= 30 for e in evs)
    lo, hi = R.wilson(k, n)
    return {"n": n, "hit": k / n * 100, "lo": lo, "hi": hi, "net": st.mean(e["ret"] for e in evs) - R.COST}


def evaluate_all(events: list[dict], freeze: str, session: str | None = None) -> list[dict]:
    evs = [e for e in events if session is None or e["ses"] == session]
    a = [e for e in evs if e["d"] < freeze]
    b = [e for e in evs if e["d"] >= freeze]
    rows = [{"name": "조건 없음", "a": summarize(a), "b": summarize(b)}]
    for n in PRESETS:
        rows.append({"name": n, "a": summarize([e for e in a if n in e["hits"]]),
                     "b": summarize([e for e in b if n in e["hits"]])})
    return rows


def text(rows: list[dict], freeze: str, session: str | None) -> str:
    from . import ruleeval as R

    def cell(s):
        if not s["n"]:
            return f"{'0건':>6s} {'':28s}"
        return f"{s['n']:5d}건 적중 {s['hit']:5.1f}% [{s['lo']:4.1f}~{s['hi']:5.1f}] 순 {s['net']:+5.1f}%"
    w = max(len(r["name"]) for r in rows) + 2
    lines = [f"해외 스크리너 검증 — 세션 {session or '전체'} · 기준일 {freeze} (앞=선택 기간, 뒤=확인 기간)",
             f"터짐 순간에 식이 맞았으면 그 분 종가에 산 것으로: +{R.TP:g}% 익절 / −{R.SL:g}% 손절 / {R.HORIZON_MIN}분, 비용 {R.COST:g}% 뺌",
             f"적중 = 3시간 안 +30% 도달 비율 [95% 범위]",
             f"{'식':{w}s}| {'앞':^48s}| {'뒤':^48s}"]
    for r in rows:
        lines.append(f"{r['name']:{w}s}| {cell(r['a'])} | {cell(r['b'])}")
    good = [r["name"] for r in rows[1:] if r["b"]["n"] >= R.MIN_HOLDOUT_N and r["a"]["n"] >= R.MIN_HOLDOUT_N
            and r["a"]["net"] > 0 and r["b"]["net"] > 0]
    lines.append("")
    lines.append("판정: " + ("두 기간 모두 따라 사서 플러스(표본 30건 이상): " + ", ".join(good) if good else
                             "두 기간 모두 따라 사서 플러스인 식은 없음 (표본 30건 이상 기준) → 알림·후보 선별용으로만"))
    return "\n".join(lines)
