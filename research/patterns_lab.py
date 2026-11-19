"""차트 패턴 연구실 (독립 연구 코드). 과거 1분봉(커버리지 ≥2,300종목인 10/20~11/11, 17거래일)에서 새 패턴 6종을 자동 탐지·백테스트.
기존 A(급등봉 즉시) · B(눌림 −5% 재돌파) · C(VWAP 지지) 와 다른 구조. 규칙은 아래 상수로 '미리 고정' — 결과를 보고 조정하지 않는다.

패턴 (신호 봉 i 가 완성된 시점까지의 봉만 사용, 진입 = i+1 봉 시가, 종목·날 패턴마다 첫 신호만)
 SB  두 번째 거래량 폭발: 첫 폭발 뒤 20분↑, 최근 15분 거래대금 ≤ 첫 폭발 5분의 25% (조용), 그 구간 고가를 종가로 돌파 + 거래 폭발
 HC  고점 횡보 돌파: 이미 +10%↑ 인 종목이 일 고점 3% 안에서 12분 폭 ≤4% 횡보(거래 감소) 뒤 거래 2배로 상단 돌파
 SUP 지지선 형성: 30분 안에 같은 저점대(±1%)를 3번↑ 터치(3분 간격↑, 사이에 +2%↑ 반등) 뒤 양봉으로 +3%↑ 반등 확인
 DRV 거래 마름 뒤 재확대: 앞 30분 활발($10만↑) → 10분 거래 ≤ 그 평균의 25% & 폭 ≤5% → 거래 4배 + 상단 종가 돌파
 CR  급락 뒤 회복: 15분 안 고점→저점 −15%↓ (거래 폭발 동반) · 저점 3분↑ 유지 · 저점 뒤 거래 ≤ 급락 구간의 50% · 38.2% 되돌림 양봉 회복
 VPP 거래량 대비 가격 유지: 큰 거래봉(양봉·상반부 마감) 뒤 3분간 저점 안 깨고 종가 유지(−2% 안) + 거래 지속(≥50%), 전날 대비 −5~+10% (아직 안 오른 단계)
청산: 구조 손절(패턴 저점 −0.2%) · 익절 2R 또는 3R · 120분 · 같은 봉이면 손절 먼저, 시가 갭이면 시가 체결. 손절폭 2~20% 만 거래(너무 좁으면 스프레드에 잠김).
포지션 = 계좌 위험 1% / 손절폭 (상한 25%). 변형 = 6패턴 × 익절 2 = 12개 (다중 검정 K=12)."""
from __future__ import annotations

import pickle
import sqlite3
import zlib
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "D:/pennybot-live/research")
import surge_study as SS

MIN_SYMS, MIN_ADV, MIN_DAY_DV = 2300, 25e3, 5e4
OUT = Path("D:/pennybot-live/state/research")
FAMILIES = ["SB", "HC", "SUP", "DRV", "CR", "VPP", "PB"]
NAMES = {"SB": "두 번째 거래량 폭발", "HC": "고점 횡보 돌파", "SUP": "지지선 형성", "DRV": "거래 마름 뒤 재확대", "CR": "급락 뒤 회복", "VPP": "거래량 대비 가격 유지", "PB": "폭발 뒤 눌림 재돌파"}


class Day:
    """한 종목·하루의 1분봉 (ts = 봉의 끝 시각). 창 계산은 항상 '신호 봉 i 직전'까지."""
    def __init__(self, ts, o, h, l, c, v, pc, vscale=1.0):
        self.ts, self.o, self.h, self.l, self.c, self.v, self.pc = ts, o, h, l, c, v, pc
        self.vscale = vscale                              # 봉 거래량이 과소 집계된 날의 보정(절대 $ 하한에만 곱함). 기본 1.0 = 기존과 동일
        self.dv = v * c
        self.cs = np.concatenate([[0.0], np.cumsum(self.dv)])
        self.n = len(c)

    def lo(self, t):                                  # ts >= t 인 첫 인덱스
        return int(np.searchsorted(self.ts, t, "left"))

    def dsum(self, a, b):                             # [a, b) 거래대금 합
        return float(self.cs[b] - self.cs[a])

    def base(self, i):                                # 직전 60분(최근 5분 제외) 봉 거래대금 중앙값, 3개 미만이면 0
        t = self.ts[i]; a, b = self.lo(t - 3600), self.lo(t - 300)
        return float(np.median(self.dv[a:b])) if b - a >= 3 else 0.0

    def burst(self, i, min_dv=5e3):                   # 거래량 폭발 판정 (V0 와 같은 정의)
        if self.dv[i] < min_dv: return False
        t = self.ts[i]
        return self.dsum(self.lo(t - 300), i + 1) >= 2e4 and self.dv[i] >= max(5 * self.base(i), min_dv)


def det_sb(d: Day):
    b1 = next((i for i in range(3, d.n - 2) if d.burst(i)), None)
    if b1 is None: return None
    t1 = d.ts[b1]; S = d.dsum(d.lo(t1 - 300), b1 + 1)
    for i in range(b1 + 1, d.n - 1):
        t = d.ts[i]
        if t - t1 < 1200: continue
        a = d.lo(t - 900)
        if d.ts[b1] + 300 > d.ts[a]: continue         # 창이 첫 폭발 5분 뒤에 시작해야 함
        if i - a < 3: continue
        q = d.dsum(a, i)
        if q > 0.25 * S: continue
        if d.dv[i] < max(5e3, 5 * q / 15, 0.1 * S) or d.dsum(d.lo(t - 300), i + 1) < 2e4: continue
        top = d.h[a:i].max()
        if d.c[i] <= top or d.c[i] < 0.85 * d.h[b1:i].max(): continue
        return dict(i=i, stop=float(d.l[a:i].min() * 0.998), w0=float(d.ts[a]), w1=float(d.ts[i - 1]), note="조용한 15분 뒤 돌파")
    return None


def det_hc(d: Day):
    for i in range(15, d.n - 1):
        t = d.ts[i]; a = d.lo(t - 720)
        if i - a < 5: continue
        top, bot = d.h[a:i].max(), d.l[a:i].min()
        if bot <= 0 or top / bot - 1 > 0.04: continue
        if top < 0.97 * d.h[:i].max() or d.c[a] / d.pc - 1 < 0.10: continue
        p0 = d.lo(t - 720 - 900)
        pv = d.dsum(p0, a)
        if pv < 2e4: continue
        wv = d.dsum(a, i)
        if wv / 12 > 0.6 * pv / 15: continue
        if d.c[i] > top and d.dv[i] >= 2 * wv / 12:
            return dict(i=i, stop=float(bot * 0.998), w0=float(d.ts[a]), w1=float(d.ts[i - 1]), note="고점 3% 안 횡보 후 돌파")
    return None


def det_sup(d: Day):
    for i in range(20, d.n - 1):
        t = d.ts[i]; a = d.lo(t - 1800)
        if i - a < 8 or d.c[i] / d.pc - 1 < 0.10 or not (d.c[i] > d.o[i]): continue
        L = d.l[a:i].min()
        if L <= 0 or d.c[i] < L * 1.03: continue
        idx = [j for j in range(a, i) if d.l[j] <= L * 1.01]
        touches = []
        for j in idx:
            if not touches or d.ts[j] - d.ts[touches[-1]] >= 180:
                if touches and d.h[touches[-1]:j + 1].max() < L * 1.02: continue   # 사이에 +2% 반등이 없으면 같은 터치
                touches.append(j)
        if len(touches) >= 3 and d.ts[touches[-1]] >= t - 600:
            return dict(i=i, stop=float(L * 0.99), w0=float(d.ts[a]), w1=float(d.ts[i - 1]), note=f"지지대 {L:.4g} 터치 {len(touches)}회")
    return None


def det_drv(d: Day):
    for i in range(20, d.n - 1):
        t = d.ts[i]; a = d.lo(t - 600); p0 = d.lo(t - 600 - 1800)
        if i - a < 3: continue
        pr = d.dsum(p0, a)
        if pr < 1e5: continue
        wd = d.dsum(a, i)
        if wd > 0.25 * pr / 3: continue
        top, bot = d.h[a:i].max(), d.l[a:i].min()
        if bot <= 0 or top / bot - 1 > 0.05: continue
        if d.c[i] > top and d.dv[i] >= max(5e3, 4 * wd / 10, 0.5 * pr / 30):
            return dict(i=i, stop=float(bot * 0.998), w0=float(d.ts[a]), w1=float(d.ts[i - 1]), note="10분 거래 마름 뒤 재확대")
    return None


def det_cr(d: Day):
    for i in range(25, d.n - 1):
        t = d.ts[i]; a0 = d.lo(t - 900)
        if i - a0 < 6: continue
        ia = a0 + int(np.argmax(d.h[a0:i + 1]));
        if ia >= i - 2: continue
        ib = ia + int(np.argmin(d.l[ia:i + 1]))
        Hw, Lw = d.h[ia], d.l[ib]
        if Lw <= 0 or Lw / Hw - 1 > -0.15 or Hw / d.pc - 1 < 0.10: continue
        if d.ts[ib] > t - 180 or ib >= i or d.l[ib + 1:i + 1].min() < Lw * 0.998: continue
        fv = d.dv[ia:ib + 1]
        b0 = d.lo(t - 3600)
        med = float(np.median(d.dv[b0:a0])) if a0 - b0 >= 3 else 0.0
        if fv.max() < max(3 * med, 1e4 * d.vscale): continue
        post = d.dv[ib + 1:i + 1]
        if len(post) < 2 or post.mean() > 0.5 * fv.mean(): continue
        if d.c[i] >= Lw + 0.382 * (Hw - Lw) and d.c[i] > d.o[i]:
            return dict(i=i, Hw=float(Hw), Lw=float(Lw), stop=float(Lw * 0.995), w0=float(d.ts[ia]), w1=float(d.ts[i - 1]), note=f"급락 {Lw/Hw-1:+.0%} 뒤 38% 회복")
    return None


def det_vpp(d: Day):
    for b in range(3, d.n - 5):
        if not d.burst(b, 1e4) or d.c[b] < d.o[b] or d.c[b] < d.l[b] + 0.5 * (d.h[b] - d.l[b]): continue
        tb = d.ts[b]
        i = next((j for j in range(b + 1, d.n - 1) if d.ts[j] >= tb + 180), None)
        if i is None or d.ts[i] > tb + 300: continue
        if d.l[b + 1:i + 1].min() < d.l[b] or d.c[b + 1:i + 1].min() < 0.98 * d.c[b]: continue
        if d.dv[b + 1:i + 1].sum() < 0.5 * d.dv[b]: continue
        g = d.c[i] / d.pc - 1
        if not (-0.05 <= g <= 0.10): continue
        return dict(i=i, stop=float(d.l[b:i + 1].min() * 0.998), w0=float(d.ts[b]), w1=float(d.ts[i]), note="큰 거래봉 뒤 3분 유지")
    return None


def det_pb(d: Day):
    """PB 폭발 뒤 −5% 눌림 후 고점 재돌파 (2026-11-15 파이프라인용으로 추가 — **과거 데이터 결과를 보지 않음**, 새 데이터 전용).
    첫 거래량 폭발 b1 뒤 30분 안에 폭발 고점(눌림 전 고점)에서 −5% 이상 눌렸다가, 그 고점을 종가로 넘는 봉. 손절 = 눌림 저점. (구조 손절 · 진입 다음 봉 시가)"""
    b1 = next((i for i in range(3, d.n - 2) if d.burst(i)), None)
    if b1 is None: return None
    t1 = d.ts[b1]; H = d.h[b1]; pulled = False; Lp = None
    for i in range(b1 + 1, d.n - 1):
        if d.ts[i] > t1 + 1800: break
        if not pulled:
            if d.l[i] <= H * 0.95: pulled, Lp = True, d.l[i]
            else: H = max(H, d.h[i])
        else:
            Lp = min(Lp, d.l[i])
            if d.c[i] > H:
                return dict(i=i, stop=float(Lp * 0.998), w0=float(d.ts[b1]), w1=float(d.ts[i - 1]), note="폭발 뒤 −5% 눌림 후 고점 재돌파")
    return None


DET = {"SB": det_sb, "HC": det_hc, "SUP": det_sup, "DRV": det_drv, "CR": det_cr, "VPP": det_vpp, "PB": det_pb}


def detect_all(d: Day):
    return {k: f for k, f in ((k, fn(d)) for k, fn in DET.items()) if f}


def simulate(d: Day, sig: dict, tp_r: float, delay: int = 0):
    """신호 봉 i → i+1+delay 봉 시가 진입, 구조 손절, tp_r R 익절, 120분. → dict 또는 None(손절폭 범위 밖·봉 없음)"""
    j0 = sig["i"] + 1 + delay
    if j0 >= d.n or d.ts[j0] - d.ts[sig["i"]] > 180 * (1 + delay): return None
    e = d.o[j0]; stop = sig["stop"]
    if not (0.10 <= e <= 10) or stop >= e: return None
    dist = (e - stop) / e
    if not (0.02 <= dist <= 0.20): return None
    tgt = e + tp_r * (e - stop); out, px, jx = "time", d.c[-1], d.n - 1
    for j in range(j0, d.n):
        if d.ts[j] > d.ts[j0] + 7200: out, px, jx = "time", d.c[j - 1], j - 1; break
        if j > j0 and d.o[j] <= stop: out, px, jx = "sl", d.o[j], j; break
        if d.l[j] <= stop: out, px, jx = "sl", stop, j; break
        if d.h[j] >= tgt: out, px, jx = "tp", tgt, j; break
    return dict(entry=float(e), exit=float(px), out=out, dist=float(dist), gross=float(px / e - 1), ts_entry=float(d.ts[j0]), ts_exit=float(d.ts[jx]), tp_r=tp_r, delay=delay, j0=j0, jx=jx)


def run(only_days=None, out_name="pattern_signals.pkl", d_from="2026-10-01"):
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SS.DB.format("market.db"), uri=True)
    cov = dict(con.execute("SELECT trade_date, COUNT(DISTINCT symbol) FROM bars_1m WHERE trade_date >= ? GROUP BY 1", (d_from,)))
    days = sorted(dd for dd, n in cov.items() if n >= MIN_SYMS and (only_days is None or dd in only_days))
    pcs = SS.load_pc(con)
    dvd = {}
    for s, dd, c, v in con.execute("SELECT symbol, d, close, volume FROM daily_bars WHERE close > 0 AND d >= '2026-08-15' ORDER BY symbol, d"):
        dvd.setdefault(s, []).append((dd, c * (v or 0)))
    adv = lambda s, td: (lambda x: sum(x) / len(x) if len(x) >= 10 else 0.0)([z for dd, z in dvd.get(s, ()) if dd < td][-20:])
    sigs, nsd = [], 0
    for td in days:
        cur, buf, groups = None, [], {}
        for s, ts, ses, o, h, l, c, v in con.execute("SELECT symbol, ts, session, open, high, low, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (td,)):
            if s != cur:
                if cur and len(buf) >= 30: groups[cur] = buf
                cur, buf = s, []
            buf.append((ts, ses, o, h, l, c, v))
        if cur and len(buf) >= 30: groups[cur] = buf
        for s, b in groups.items():
            pc = SS.prev_close(pcs, s, td)
            if not pc or not (0.05 <= pc <= 10) or adv(s, td) < MIN_ADV: continue
            ts = np.array([datetime.fromisoformat(x[0]).timestamp() for x in b]); a = np.array([x[2:] for x in b], float)
            d = Day(ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], pc)
            if d.cs[-1] < MIN_DAY_DV: continue
            nsd += 1
            found = detect_all(d)
            rr = np.random.default_rng(zlib.crc32(f"{s}{td}".encode()))        # 대조군: 같은 종목·날 무작위 한 봉, 같은 방식 구조 손절(직전 10봉 저점)
            if rr.random() < 0.15 and d.n > 40:
                ri = int(rr.integers(20, d.n - 3))
                found["RND"] = dict(i=ri, stop=float(d.l[max(0, ri - 10):ri + 1].min() * 0.998), w0=float(ts[max(0, ri - 10)]), w1=float(ts[ri]), note="무작위 진입(대조군)")
            for fam, sg in found.items():
                ses = SS.SES.get(b[sg["i"]][1], 0)
                for tp_r in (2.0, 3.0):
                    for delay in (0, 1):
                        r = simulate(d, sg, tp_r, delay)
                        if r: sigs.append(dict(fam=fam, sym=s, td=td, ses=ses, sig_ts=float(ts[sg["i"]]), stop=sg["stop"], w0=sg["w0"], w1=sg["w1"], note=sg["note"], **r))
        print(td, "신호", len(sigs), "· 누적 종목·날", nsd, flush=True)
    with open(OUT / out_name, "wb") as fh:
        pickle.dump(dict(sigs=sigs, days=days, n_symbol_days=nsd), fh)
    return sigs


if __name__ == "__main__":
    s = run()
    print("완료 거래 후보", len(s))
