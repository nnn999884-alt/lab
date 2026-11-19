"""감지기 재생 검증: 과거 1분봉에서 TradingView 스냅샷(:10, :50 초, 현재가·세션 누적 거래량)을 합성해 TriggerDetector 에 흘리고, 소급 연구(early_study)의 V0 트리거와 비교.
(합성 스냅샷이라 TV 근사 오차는 없음 — '감지 논리가 같은 정의인지'만 확인.)"""
import pickle, sqlite3, sys
from collections import defaultdict
from datetime import datetime
import numpy as np
sys.path.insert(0, "D:/pennybot-live")
from pennybot.collector import triggerlog as TL
DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-11-10"
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
class Q:  # Store.query 흉내
    def query(self, sql, args=()): return con.execute(sql, args).fetchall()
ref = TL.load_ref(Q(), DAY)
ep = lambda s: datetime.fromisoformat(s).timestamp()
bars = defaultdict(dict)   # sym -> {minute index: (end_ts, session, o, c, v)}
for s, ts, ses, o, c, v in con.execute("SELECT symbol, ts, session, open, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (DAY,)):
    e = ep(ts); bars[s][int(e // 60) - 1] = (e, ses, o, c, v or 0)
mins = sorted({m for b in bars.values() for m in b}); m0, m1 = mins[0], mins[-1]
det = TL.TriggerDetector(); det.reset(DAY, ref)
state = {s: dict(cum=0.0, px=None, ses=None, seen=False) for s in bars}
live = []
for m in range(m0, m1 + 1):
    snap10, snap50 = {}, {}
    for s, b in bars.items():
        st = state[s]; bar = b.get(m)
        if bar:
            st["seen"] = True
            snap10[s] = (st["cum"], bar[2], bar[1])                 # 분 시작: 직전까지 누적, 시가
            st["cum"] += bar[4]; st["px"] = bar[3]; st["ses"] = bar[1]
            snap50[s] = (st["cum"], bar[3], bar[1])
        elif st["seen"]:
            snap10[s] = snap50[s] = (st["cum"], st["px"], st["ses"])
    for sec, snap in ((10, snap10), (50, snap50)):
        for ses in ("PRE", "REGULAR", "AFTER", "DAY"):
            part = {s: ({"px": px, "pre_vol": cum, "vol": cum, "post_vol": cum}) for s, (cum, px, sx) in snap.items() if sx == ses and px}
            if part: live += det.feed(part, m * 60 + sec, ses)
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
ws = det.warm_start
print("감지기 워밍업 시작(첫 PRE 스냅샷):", datetime.utcfromtimestamp(ws).strftime("%H:%M UTC"), "· 워밍업 중(warm_s<3600) 트리거", sum(t["warm_s"] < 3600 for t in live), "건 (로거는 호가를 요청하지 않고 'unconfirmed_warmup' 으로만 기록)")
live = [t for t in live if t["warm_s"] >= 3600]                                   # 로거가 호가를 요청하는 트리거만
retro = {e["sym"]: e for e in EV if e["td"] == DAY and e["ses"] != 0 and e["ts_trig"] - ws >= 3600}   # 같은 기준(워밍업 이후)의 소급 트리거만 비교
lv = {t["symbol"]: t for t in live}
exact = [s for s in retro if s in lv and abs(lv[s]["bar_end_ts"] - retro[s]["ts_trig"]) < 1]
near = [s for s in retro if s in lv and abs(lv[s]["bar_end_ts"] - retro[s]["ts_trig"]) <= 180]
print(f"{DAY}: 소급 V0 트리거(데이마켓 제외) {len(retro)} · 재생 감지 {len(lv)} · 같은 봉 {len(exact)} ({len(exact)/max(len(retro),1)*100:.0f}%) · 3분 안 {len(near)} ({len(near)/max(len(retro),1)*100:.0f}%)")
allretro = {e["sym"]: e for e in EV if e["td"] == DAY}                                # 필터 없는 소급 첫 트리거(데이마켓 포함)
miss = [s for s in retro if s not in lv]; extra = [s for s in lv if s not in retro]
earlier = [s for s in extra if s in allretro and allretro[s]["ts_trig"] < lv[s]["bar_end_ts"] - 1]   # 소급에서 이미 더 일찍 첫 트리거가 난 종목 (소급은 종목·날 첫 번째만 셈)
true_extra = [s for s in extra if s not in earlier]
def has_next(sym, t):                                                                  # 소급 코드는 '다음 봉이 3분 안에 있는' 첫 트리거만 사건으로 셈 (미래 정보)
    m = int(t // 60) - 1
    return any((m + k) in bars[sym] for k in (1, 2, 3))
nonext = [s_ for s_ in true_extra if not has_next(s_, lv[s_]["bar_end_ts"])]
print(f"  └ 진짜 오탐 후보 {len(true_extra)} 중 '다음 봉이 3분 안에 없음'(소급은 그 종목·날을 통째로 버림) {len(nonext)} · 나머지 {len(true_extra)-len(nonext)}")
rest = [s_ for s_ in true_extra if s_ not in nonext]
import collections
print("    나머지 세션:", collections.Counter(lv[s_]["session"] for s_ in rest), "· 시각(UTC 시):", sorted(collections.Counter(int((lv[s_]["bar_end_ts"] % 86400) // 3600) for s_ in rest).items()))
print(f"  '재생에만 있음' {len(extra)} 중: 소급에서는 같은 종목이 더 일찍(워밍업·데이마켓 구간) 이미 트리거 → 의미 차이 {len(earlier)} · 소급에 그 종목 트리거가 전혀 없음 {len(true_extra)}")
print(f"  소급에만 있음 {len(miss)} · 재생에만 있음 {len(extra)}")
for s in miss[:6]:
    e = retro[s]; print("   소급만:", s, datetime.utcfromtimestamp(e["ts_trig"]).strftime("%H:%M"), "세션", e["ses"], "dv5 %.0f" % e["feats"]["dv5"], "up_pc %.3f" % e["feats"]["up_pc"])
for s in true_extra[:6]:
    t = lv[s]; print("   재생만:", s, datetime.utcfromtimestamp(t["bar_end_ts"]).strftime("%H:%M"), t["session"], "dv_bar %.0f dv5 %.0f" % (t["dv_bar"], t["dv5"]))
pickle.dump(live, open("D:/pennybot-live/state/research/replay_live_" + DAY + ".pkl", "wb"))
