"""조기 감지 일꾼: 가격이 크게 오르기 *전에* 거래가 붙기 시작하는 순간을 체결(틱) 단위로 잡는다.
- 종목마다 기준선: 최근 30분 분당 거래대금(EWMA, 거래 없던 분은 0 으로 셈 → 조용하던 종목이 깨어날 때 배수가 크게 잡힘).
- 최근 win 초 창에서 거래대금·체결 건수·가격 변화를 보고, 30분 저점 대비 아직 얼마 안 올랐을 때만(이미 오른 뒤는 제외) 신호.
- 규칙(Variant) 여러 개를 동시에 돌려 전부 저장 → `grade` 로 비교. 알림은 notify=True 인 규칙만. 기준을 하나만 고르지 않는다(LESSONS A-3·E절).
틱마다 하는 일은 O(1) 갱신뿐이고, 세션 판정·창 스캔은 사전 조건을 통과한 때만 한다."""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

from ..core.sessions import classify, trading_day
from ..hub.client import HubClient
from . import heartbeat

BASE_MINUTES = 30
ALPHA = 2 / (BASE_MINUTES + 1)
MAX_SNAP = 200               # 신호에 같이 저장하는 직전 체결 수 상한
MIN_BASE = 1500.0            # 기준선 하한($/분): 거의 거래 없던 종목의 배수가 무한대로 튀는 것을 막음


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    win_s: int = 60
    min_dv: float = 50_000        # 창 안 거래대금 하한 ($)
    min_ratio: float = 5.0        # 창 거래대금 / (기준선×창길이) 배수
    min_trades: int = 8
    min_move: float = 2.0         # 창 안 가격 변화 하한 (%)
    max_move: float = 1e9         # 창 안 가격 변화 상한 (%) — stir 는 거래만 먼저라 +1% 미만
    max_from_low: float = 8.0     # 30분 저점 대비 상승 상한 (%) — 이미 오른 뒤는 제외
    cooldown_s: int = 1800
    skip_open_s: int = 60         # 세션 시작 직후 N초는 개장 쏠림(U자 거래량)이라 제외
    notify: bool = False


DEFAULT_VARIANTS = (
    Variant("fast", "ignite:fast", notify=True),
    Variant("early", "ignite:early", win_s=30, min_dv=25_000, min_ratio=8.0, min_trades=6, min_move=1.0,
            max_from_low=5.0, skip_open_s=0),
    Variant("strict", "ignite:strict", min_dv=100_000, min_ratio=8.0, min_trades=15, min_move=3.0, max_from_low=10.0),
    Variant("stir", "stir", min_dv=40_000, min_ratio=6.0, min_trades=10, min_move=-1.0, max_move=1.0, max_from_low=8.0),
)


class _Sym:
    __slots__ = ("dq", "dv60", "n60", "minute", "mdv", "mlow", "base", "lows", "last_sig", "warmed")

    def __init__(self) -> None:
        self.dq: deque = deque()
        self.dv60 = 0.0
        self.n60 = 0
        self.minute: int | None = None
        self.mdv = 0.0
        self.mlow = float("inf")
        self.base: float | None = None
        self.lows: deque = deque()          # (분, 그 분의 저가)
        self.last_sig: dict[str, float] = {}
        self.warmed = False


def _session(ts: float):
    t = datetime.fromtimestamp(ts, timezone.utc)
    td, ss = classify(t)
    if td is None:
        return ss, None
    return ss, ts - trading_day(td).sessions[ss][0].timestamp()


class IgniteDetector:
    def __init__(self, variants=DEFAULT_VARIANTS, price_range=(0.0, 1e9), pseudo: bool = False):
        self.variants = tuple(variants)
        self.lo, self.hi = price_range
        self.max_win = max(v.win_s for v in self.variants)
        self.min_dv_all = min(v.min_dv for v in self.variants)
        self.pseudo = pseudo                       # 1분봉을 쪼갠 가짜 틱으로 돌리는 백테스트 (체결 건수 기준을 낮춤)
        self.state: dict[str, _Sym] = defaultdict(_Sym)

    # ---- 기준선 ----
    def _close_minute(self, st: _Sym, minute: int, dv: float, low: float | None) -> None:
        st.base = dv if st.base is None else ALPHA * dv + (1 - ALPHA) * st.base
        if low is not None and low < float("inf"):
            st.lows.append((minute, low))
        while st.lows and st.lows[0][0] < minute - BASE_MINUTES:
            st.lows.popleft()

    def warm(self, symbol: str, bars: list[tuple]) -> None:
        """이미 끝난 1분봉 [(분 epoch//60, 저가, 종가, 거래량)] 으로 기준선·저점을 미리 채운다 (오래된 것부터)."""
        st = self.state[symbol]
        for minute, low, close, vol in bars:
            self._close_minute(st, minute, close * vol, low)
        st.warmed = True

    # ---- 틱 ----
    def on_tick(self, sym: str, ts: float, p: float, v: float) -> list[dict]:
        if v <= 0 or p <= 0:
            return []
        st = self.state[sym]
        m = int(ts // 60)
        if st.minute is None:
            st.minute = m
        elif m < st.minute:
            return []                               # 늦게 도착한 옛 틱
        elif m > st.minute:
            self._close_minute(st, st.minute, st.mdv, st.mlow)
            for k in range(1, min(m - st.minute, BASE_MINUTES)):      # 틱이 없던 분 = 거래대금 0
                self._close_minute(st, st.minute + k, 0.0, None)
            st.minute, st.mdv, st.mlow = m, 0.0, float("inf")
        dv = p * v
        st.mdv += dv
        st.mlow = min(st.mlow, p)
        st.dq.append((ts, p, v))
        st.dv60 += dv
        st.n60 += 1
        cut = ts - self.max_win
        while st.dq[0][0] < cut:
            _, op, ov = st.dq.popleft()
            st.dv60 -= op * ov
            st.n60 -= 1
        if st.dv60 < self.min_dv_all or not (self.lo <= p <= self.hi):
            return []
        return self._evaluate(sym, st, ts, p)

    def _evaluate(self, sym: str, st: _Sym, ts: float, p: float) -> list[dict]:
        out: list[dict] = []
        low30 = min([l for _, l in st.lows] + [st.mlow, p])
        from_low = (p / low30 - 1) * 100
        base = max(st.base or 0.0, MIN_BASE)
        sess = None
        for v in self.variants:
            if ts - st.last_sig.get(v.name, -1e18) < v.cooldown_s or from_low > v.max_from_low:
                continue
            dv = n = 0.0
            first = p
            for t, pp, vv in reversed(st.dq):
                if t < ts - v.win_s:
                    break
                dv += pp * vv
                n += 1
                first = pp
            need_trades = min(v.min_trades, 4) if self.pseudo else v.min_trades
            move = (p / first - 1) * 100
            ratio = dv / (base * v.win_s / 60)
            if dv < v.min_dv or n < need_trades or ratio < v.min_ratio or not (v.min_move <= move < v.max_move):
                continue
            if sess is None:
                sess = _session(ts)
            ss, since_open = sess
            if v.skip_open_s and since_open is not None and since_open < v.skip_open_s:
                continue
            st.last_sig[v.name] = ts
            recent = [[round(t - ts, 2), pp, vv] for t, pp, vv in list(st.dq)[-MAX_SNAP:]]      # 신호 직전 체결(상대 시각 초) — 나중에 틱 단위로 규칙을 조정하려는 기록
            attrs = {"variant": v.name, "win_s": v.win_s, "dv": round(dv), "ratio": round(ratio, 1), "trades": int(n),
                     "move_pct": round(move, 2), "from_low_pct": round(from_low, 2), "base_dv_min": round(base),
                     "session": ss, "sec_from_open": None if since_open is None else round(since_open), "pseudo": self.pseudo,
                     "ticks": recent}
            head = "👀 거래만 먼저 늘어남" if v.kind == "stir" else "🔥 거래 터짐 시작"
            out.append({"s": sym, "ts": ts, "kind": v.kind, "value": move, "price": p, "attrs": attrs, "notify": v.notify,
                        "text": (f"{head} {sym} ${p:.4f}\n최근 {v.win_s}초 ${dv:,.0f} (평소 분당의 {ratio:.0f}배 · 체결 {int(n)}건) · "
                                 f"{move:+.1f}% · 30분 저점 대비 +{from_low:.1f}% (아직 이른 구간)\n(규칙 '{v.name}' — 효과 미검증, 채점 중)")})
        return out


def run(cfg, variants=DEFAULT_VARIANTS) -> None:
    from ..core.store import Store
    from ..hub.client import HubClient
    from .surge import enrich
    det = IgniteDetector(variants, (cfg.min_price, cfg.max_price))
    store = Store(cfg.db_path)
    cands: dict[str, dict] = {}
    client: HubClient
    stats = {"ticks": 0, "signals": 0}

    def warm_from_store(sym: str) -> None:
        try:
            rows = store.bars(sym, BASE_MINUTES)
            det.warm(sym, [(int(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp() // 60), r["low"], r["close"], r["volume"])
                           for r in rows])
        except Exception:
            det.state[sym].warmed = True

    def on_msg(topic: str, d: dict) -> None:
        if topic == "candidates":
            cands.clear()
            cands.update({i["s"]: i for i in d.get("items", ())})
            return
        sym = d["s"]
        if not det.state[sym].warmed:
            warm_from_store(sym)
        stats["ticks"] += 1
        for sig in det.on_tick(sym, d["ts"], d["p"], d["v"]):
            stats["signals"] += 1
            client.publish("signal", enrich(sig, cands.get(sym)))

    client = HubClient(cfg.hub_port, ["tick", "candidates"], on_msg)
    heartbeat.start(client, "ignite", lambda: dict(stats))
    threading.Event().wait()


# ---------------- 과거 1분봉으로 돌려 보기 (백테스트) ----------------
def pseudo_ticks(ts: float, o: float, h: float, l: float, c: float, v: float) -> list[tuple]:
    """1분봉 하나를 15초 간격 4개 틱으로 쪼갠다 (양봉: 시→저→고→종, 음봉: 시→고→저→종). 거래량은 균등 분할.
    실제 체결 순서·건수는 알 수 없으므로 근사임 — 규칙끼리·늦은 감지와의 '상대 비교'용이고 절대 성과로 믿지 말 것."""
    path = (o, l, h, c) if c >= o else (o, h, l, c)
    return [(ts + 15 * i + 14, p, v / 4) for i, p in enumerate(path)]


def backtest(db_path, out_path, since: str | None = None, max_symbols: int | None = None,
             variants=DEFAULT_VARIANTS, warmup_bars: int = 30, price_range=(0.0, 1e9)) -> dict:
    """저장된 1분봉(bars_1m)을 종목별로 시간순으로 흘려 조기 감지 신호와, 비교용 '늦은 감지'(기존 5분 +10%) 신호를 out_path 의 signals 표에 저장.
    종목·거래일마다 처음 warmup_bars 개 봉은 기준선을 쌓는 데만 쓰고 신호를 내지 않는다(기준선 없이 터짐으로 오인하는 것 방지)."""
    import sqlite3
    from pathlib import Path

    from ..core.store import Store
    from .surge import SurgeDetector
    src = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    out = Store(out_path)
    out.db.execute("DELETE FROM signals")
    q = "SELECT symbol, ts, open, high, low, close, volume FROM bars_1m"
    args: list = []
    if since:
        q += " WHERE ts >= ?"
        args.append(since)
    syms = [r[0] for r in src.execute(f"SELECT DISTINCT symbol FROM ({q}) ORDER BY symbol", args)]
    if max_symbols:
        syms = syms[:max_symbols]
    counts: dict[str, int] = defaultdict(int)
    for sym in syms:
        det = IgniteDetector(variants, price_range, pseudo=True)
        late = SurgeDetector(min_price=price_range[0], max_price=price_range[1])
        seen, last_t = 0, None
        rows_q = q + (" AND" if since else " WHERE") + " symbol=? ORDER BY ts"
        for _, ts, o, h, l, c, v in src.execute(rows_q, [*args, sym]):
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if last_t is not None and t - last_t > 3600:        # 거래일이 바뀌면 기준선을 다시 쌓는다
                seen = 0
            last_t = t
            seen += 1
            if not (o and h and l and c) or v is None:
                continue
            sigs = []
            for tt, p, vv in pseudo_ticks(t, o, h, l, c, v):
                sigs += det.on_tick(sym, tt, p, vv)
            ls = late.feed(sym, t + 60, c)
            if ls and seen > warmup_bars:
                ls["kind"] = "surge"
                sigs.append(ls)
            if seen <= warmup_bars:
                continue
            for s in sigs:
                out.add_signal(datetime.fromtimestamp(s["ts"], timezone.utc).isoformat(), sym, s["kind"], s.get("price"),
                               s.get("value"), None, s["text"], s.get("attrs"))
                counts[s["kind"]] += 1
    return dict(counts)


def main_backtest(argv: list[str]) -> int:
    from pathlib import Path
    path = next((a for a in argv if not a.startswith("--")), None)
    opt = {a.split("=")[0]: a.split("=")[1] for a in argv if a.startswith("--") and "=" in a}
    if not path or not Path(path).exists():
        print("사용: python -m pennybot2 backtest-ignite DB경로 [--out=state/backtest_signals.db] [--since=2026-10-15] [--max-symbols=N]")
        return 2
    out = opt.get("--out", "state/backtest_signals.db")
    counts = backtest(path, out, opt.get("--since"), int(opt["--max-symbols"]) if "--max-symbols" in opt else None)
    print("신호 수:", counts or "없음")
    print(f"저장: {out}\n다음 → 같은 기준으로 채점·비교:  python -m pennybot2 grade {path} --alerts-db={out}\n"
          "(1분봉을 4개 가짜 틱으로 쪼갠 근사이므로 규칙끼리·'surge'(늦은 감지)와의 상대 비교로만 볼 것)")
    return 0
