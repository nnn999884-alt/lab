"""조건검색식 — 이동평균 정배열·볼린저·엔벨로프 같은 기술적 조건을 직접 걸어서 종목을 찾는다.

급등 전조 알림(체결 흐름 기준)과는 별개로, 키움 조건검색처럼 "식"을 따로 돌린다.
식 두 개(급등이·벌떡이)는 US_Stock_searcher 의 조건표(급등이·벌떡이)를 옮긴 것이다. 우리가 가진 1분봉(bars_1m)을
5분·15분·60분으로 묶어서 계산한다.

- 데이터가 모자란 조건(예: 60분봉 MA250 은 250시간치 필요)은 '건너뜀' — 실패로 치지 않고 따로 센다.
- 식은 `state/condscan.json` 으로 덮어쓸 수 있다: {"식이름": {"conds": [...]}} (형식은 PRESETS 참고).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

# 조건 종류
#  ma_align  : tf 분봉, ma[0] >= ma[1] >= ma[2]
#  ma_ge     : tf 분봉, ma[0] >= ma[1]  (tf=1440 이면 일봉)
#  ma_gap    : tf 분봉, |MA a − MA b| / MA b <= pct %
#  bb_break  : tf 분봉 볼린저 상단 상향돌파 (직전 봉은 아래, 지금 봉은 위)
#  bb_above  : tf 분봉 볼린저 상단 이상
#  env_break : tf 분봉 엔벨로프(n, pct%) 상단 상향돌파
#  day_vol   : 오늘 거래량 범위
#  mcap      : 시가총액(달러) 이상 — 상장주식수를 모르면 건너뜀
PRESETS: dict[str, dict] = {
    "추세 돌파": {"label": "추세 돌파", "desc": "5분봉이 정배열이고 볼린저·엔벨로프 상단을 막 뚫은 종목 — 이미 오르기 시작한 종목", "min_ok": 10, "conds": [
        {"t": "ma_align", "tf": 5, "ma": [5, 20, 60]},
        {"t": "ma_align", "tf": 5, "ma": [5, 20, 200]},
        {"t": "ma_align", "tf": 5, "ma": [5, 100, 150]},
        {"t": "bb_break", "tf": 5, "n": 20, "k": 2.0},
        {"t": "bb_above", "tf": 5, "n": 20, "k": 2.0},
        {"t": "day_vol", "min": 300_000},
        {"t": "ma_gap", "tf": 5, "ma": [5, 200], "pct": 10},
        {"t": "ma_gap", "tf": 5, "ma": [5, 20], "pct": 5},
        {"t": "mcap", "min": 30e6},
        {"t": "ma_ge", "tf": 5, "ma": [5, 224]},
        {"t": "env_break", "tf": 5, "n": 12, "pct": 2.2},
        {"t": "env_break", "tf": 5, "n": 20, "pct": 3.3},
        {"t": "ma_align", "tf": 60, "ma": [5, 20, 60]},
        {"t": "ma_align", "tf": 60, "ma": [20, 60, 112]},
        {"t": "ma_align", "tf": 60, "ma": [20, 112, 250]},
    ]},
    "다중 정배열": {"label": "다중 정배열", "desc": "1·5·15분봉 이동평균선이 모두 정배열인 종목 — 추세가 이어지는 종목", "min_ok": 8, "conds": [
        {"t": "day_vol", "min": 300_000},
        {"t": "mcap", "min": 22e6},
        {"t": "ma_align", "tf": 1, "ma": [5, 60, 112]},
        {"t": "ma_align", "tf": 1, "ma": [5, 112, 224]},
        {"t": "ma_align", "tf": 5, "ma": [5, 20, 60]},
        {"t": "ma_align", "tf": 5, "ma": [60, 100, 200]},
        {"t": "ma_gap", "tf": 5, "ma": [5, 224], "pct": 10},
        {"t": "ma_align", "tf": 15, "ma": [5, 20, 60]},
        {"t": "ma_align", "tf": 15, "ma": [20, 60, 200]},
        {"t": "ma_ge", "tf": 1440, "ma": [5, 10]},
        {"t": "env_break", "tf": 5, "n": 12, "pct": 3.0},
        {"t": "env_break", "tf": 1, "n": 12, "pct": 2.0},
    ]},
}

MAX_1M = 3200          # 60분 MA60(=3600분) 까지는 못 보지만 5분 MA224(1120분)·15분 MA200(3000분)은 본다
_cache: dict[str, tuple[float, dict]] = {}
_running: set[str] = set()
_lock = threading.Lock()   # 같은 검사를 여러 요청이 동시에 돌리지 않게 (3초마다 오는 화면 요청이 쌓이던 문제)


def cond_text(c: dict) -> str:
    t = c["t"]
    tf = {1: "1분", 5: "5분", 15: "15분", 60: "60분", 1440: "일"}.get(c.get("tf", 0), f"{c.get('tf')}분")
    if t == "ma_align":
        return f"{tf} 정배열 " + " ≥ ".join(f"MA{m}" for m in c["ma"])
    if t == "ma_ge":
        return f"{tf} MA{c['ma'][0]} ≥ MA{c['ma'][1]}"
    if t == "ma_gap":
        return f"{tf} MA{c['ma'][0]}↔MA{c['ma'][1]} 이격 {c['pct']:g}% 이내"
    if t == "bb_break":
        return f"{tf} 볼린저({c['n']},{c['k']:g}) 상단 돌파"
    if t == "bb_above":
        return f"{tf} 볼린저({c['n']},{c['k']:g}) 상단 위"
    if t == "env_break":
        return f"{tf} 엔벨로프({c['n']},{c['pct']:g}%) 상단 돌파"
    if t == "day_vol":
        return f"오늘 거래량 {c['min']:,.0f}주 이상"
    if t == "mcap":
        return f"시총 ${c['min'] / 1e6:g}M 이상"
    return t


# ---------- 지표 ----------
def resample(bars: list[tuple], n: int) -> list[tuple]:
    """1분봉 [(epoch분, o,h,l,c,v)] → n분봉. 마지막(진행 중) 봉도 포함."""
    if n <= 1:
        return bars
    out: list[list] = []
    cur = None
    for m, o, h, l, c, v in bars:
        k = m // n
        if cur is None or cur[0] != k:
            cur = [k, o, h, l, c, v]
            out.append(cur)
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += v
    return [tuple(x) for x in out]


def _sma(closes: list[float], n: int, back: int = 0) -> float | None:
    end = len(closes) - back
    if n <= 0 or end < n:
        return None
    return sum(closes[end - n:end]) / n


def _bb_upper(closes, n, k, back=0):
    end = len(closes) - back
    if end < n:
        return None
    w = closes[end - n:end]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return m + k * sd


def _env_upper(closes, n, pct, back=0):
    m = _sma(closes, n, back)
    return None if m is None else m * (1 + pct / 100)


def evaluate(cond: dict, ctx: dict) -> bool | None:
    """True/False, 데이터가 모자라면 None(건너뜀)."""
    t = cond["t"]
    if t == "day_vol":
        v = ctx["day_vol"]
        return None if v is None else cond.get("min", 0) <= v <= cond.get("max", 1e12)
    if t == "mcap":
        sh, px = ctx.get("shares"), ctx.get("price")
        return None if not sh or not px else sh * px >= cond["min"]
    tf = cond["tf"]
    cl = ctx["closes"](tf)
    if t in ("ma_align", "ma_ge"):
        vals = [_sma(cl, m) for m in cond["ma"]]
        if any(v is None for v in vals):
            return None
        return all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
    if t == "ma_gap":
        a, b = (_sma(cl, m) for m in cond["ma"])
        if a is None or b is None or b <= 0:
            return None
        return abs(a - b) / b * 100 <= cond["pct"]
    if t in ("bb_break", "bb_above"):
        u0 = _bb_upper(cl, cond["n"], cond["k"])
        if u0 is None:
            return None
        if t == "bb_above":
            return cl[-1] >= u0
        u1 = _bb_upper(cl, cond["n"], cond["k"], back=1)
        return None if u1 is None or len(cl) < 2 else (cl[-2] <= u1 and cl[-1] >= u0)
    if t == "env_break":
        u0 = _env_upper(cl, cond["n"], cond["pct"])
        u1 = _env_upper(cl, cond["n"], cond["pct"], back=1)
        if u0 is None or u1 is None or len(cl) < 2:
            return None
        return cl[-2] <= u1 and cl[-1] >= u0
    return None


def check(bars_1m: list[tuple], conds: list[dict], day_vol=None, shares=None, daily=None) -> list[bool | None]:
    cache: dict[int, list[float]] = {}

    def closes(tf: int) -> list[float]:
        if tf not in cache:
            cache[tf] = list(daily or []) if tf >= 1440 else [b[4] for b in resample(bars_1m, tf)]
        return cache[tf]

    ctx = {"closes": closes, "day_vol": day_vol, "shares": shares, "price": bars_1m[-1][4] if bars_1m else None}
    return [evaluate(c, ctx) for c in conds]


def trend_of(bars_1m: list[tuple]) -> dict | None:
    """5분봉 이동평균으로 본 지금 추세. 상승 = MA5 > MA20 이고 현재가 ≥ MA20, 하락 = MA5 < MA20 이고 현재가 < MA20, 나머지 횡보.
    5분봉이 20개(100분) 미만이면 None. ret30 = 30분 전 대비 등락(%)."""
    if len(bars_1m) < 30:
        return None
    c5 = [b[4] for b in resample(bars_1m, 5)]
    m5, m20 = _sma(c5, 5), _sma(c5, 20)
    if m5 is None or m20 is None:
        return None
    px = bars_1m[-1][4]
    d = "up" if m5 > m20 and px >= m20 else "down" if m5 < m20 and px < m20 else "flat"
    ago = next((b[4] for b in reversed(bars_1m) if b[0] <= bars_1m[-1][0] - 30), None)
    return {"d": d, "ret30": round((px / ago - 1) * 100, 1) if ago else None}


# ---------- 스캔 ----------
def presets(state_dir: Path | None = None) -> dict[str, dict]:
    p = dict(PRESETS)
    if state_dir:
        f = Path(state_dir) / "condscan.json"
        if f.exists():
            try:
                p.update(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return p


def _epoch_min(ts: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(ts).timestamp() // 60)


def scan(market_db, name: str, state_dir: Path | None = None, *, recent_min: int = 15, limit: int = 60,
         ttl: float = 45.0) -> dict:
    """최근 recent_min 분 안에 거래된 종목 전체에 식을 돌려, 맞는 순서대로 돌려준다."""
    key = f"{market_db}|{name}"
    hit = _cache.get(key)
    if ttl <= 0:                       # 테스트·수동 실행: 기다려서 바로 계산
        return _scan(market_db, name, state_dir, key, recent_min, limit)
    if hit:                            # 저장된 결과를 바로 주고, 오래됐으면 뒤에서 새로 계산
        if time.time() - hit[0] >= ttl:
            _refresh(market_db, name, state_dir, key, recent_min, limit)
        return hit[1]
    pre = presets(state_dir)
    if name not in pre:
        return {"error": f"식 없음: {name}", "names": list(pre)}
    _refresh(market_db, name, state_dir, key, recent_min, limit)      # 처음엔 계산 중이라고만 답한다
    return {"loading": True, "name": name, "names": list(pre), "rows": [], "conds": [cond_text(c) for c in pre[name]["conds"]]}


def _refresh(market_db, name, state_dir, key, recent_min, limit) -> None:
    with _lock:
        if key in _running:
            return
        _running.add(key)

    def work():
        try:
            _scan(market_db, name, state_dir, key, recent_min, limit)
        except Exception:
            pass
        finally:
            with _lock:
                _running.discard(key)
    threading.Thread(target=work, name="condscan", daemon=True).start()


def _scan(market_db, name, state_dir, key, recent_min, limit) -> dict:
    pre = presets(state_dir)
    if name not in pre:
        return {"error": f"식 없음: {name}", "names": list(pre)}
    spec = pre[name]
    conds = spec["conds"]
    min_ok = spec.get("min_ok", max(1, len(conds) // 2))
    con = sqlite3.connect(f"file:{Path(market_db).as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        last = con.execute("SELECT MAX(ts) FROM bars_1m").fetchone()[0]
        if not last:
            out = {"name": name, "label": spec.get("label", name), "desc": spec.get("desc", ""), "rows": [],
                   "conds": [cond_text(c) for c in conds], "pass_n": [0] * len(conds), "tested": 0, "min_ok": min_ok,
                   "scanned": 0, "names": list(pre)}
            _cache[key] = (time.time(), out)
            return out
        cand = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM bars_1m WHERE volume > 0 AND ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00', ?, ?)",
            (last.replace("T", " ")[:19], f"-{recent_min} minutes"))]
        td = con.execute("SELECT trade_date FROM bars_1m WHERE ts=? LIMIT 1", (last,)).fetchone()[0]
        vols = dict(con.execute("SELECT symbol, SUM(volume) FROM bars_1m WHERE trade_date=? GROUP BY symbol", (td,)))
        shares = dict(con.execute("SELECT symbol, shares FROM stock_info"))
        try:
            names = dict(con.execute("SELECT symbol, name FROM universe WHERE name IS NOT NULL ORDER BY snapshot_date"))
        except sqlite3.Error:
            names = {}
        rows = []
        pass_n = [0] * len(conds)
        tested = 0
        for s in cand:
            raw = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? "
                              "ORDER BY ts DESC LIMIT ?", (s, MAX_1M)).fetchall()
            if len(raw) < 30:
                continue
            bars = [(_epoch_min(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in reversed(raw)]
            dc = [r[0] for r in con.execute('SELECT close FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT 30', (s,))][::-1]
            res = check(bars, conds, vols.get(s), shares.get(s), dc)
            tested += 1
            for i, r in enumerate(res):
                pass_n[i] += r is True
            ok = sum(1 for r in res if r is True)
            bad = [i for i, r in enumerate(res) if r is False]
            skip = sum(1 for r in res if r is None)
            if ok < min_ok:
                continue
            px = bars[-1][4]
            ago5 = next((b[4] for b in reversed(bars) if b[0] <= bars[-1][0] - 5), None)
            ago60 = next((b[4] for b in reversed(bars) if b[0] <= bars[-1][0] - 60), None)
            sh = shares.get(s)
            rows.append({"symbol": s, "name": names.get(s) or "", "price": px, "ok": ok, "skip": skip, "total": len(conds),
                         "full": not bad, "res": [1 if r is True else 0 if r is False else -1 for r in res],
                         "day_vol": vols.get(s) or 0, "shares": sh, "mcap": round(sh * px) if sh else None,
                         "turn": round((vols.get(s) or 0) / sh * 100, 2) if sh else None,
                         "c5": round((px / ago5 - 1) * 100, 1) if ago5 else None,
                         "c60": round((px / ago60 - 1) * 100, 1) if ago60 else None,
                         "last": raw[0][0]})
        prev = {}
        for r in rows:
            row = con.execute("SELECT close FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT 2", (r["symbol"],)).fetchall()
            if len(row) == 2 and row[1][0]:
                prev[r["symbol"]] = row[1][0]
        for r in rows:
            p = prev.get(r["symbol"])
            r["chg"] = round((r["price"] / p - 1) * 100, 1) if p else None
        rows.sort(key=lambda r: (not r["full"], -r["ok"], -(r["day_vol"] or 0)))
        out = {"name": name, "label": spec.get("label", name), "desc": spec.get("desc", ""), "asof": last, "scanned": len(cand),
               "conds": [cond_text(c) for c in conds], "pass_n": pass_n, "tested": tested, "min_ok": min_ok, "names": list(pre), "rows": rows[:limit]}
    finally:
        con.close()
    _cache[key] = (time.time(), out)
    return out
