"""다른 날 데이터 준비 (10/20~11/11 제외). 모집단 = 일봉(전 종목·편향 없음)에서 '그날 고가 ≥ 전날 종가 +30%' 인 종목-날 (전날 종가 $0.05~$10, 20일 평균 거래대금 ≥$25k).
CR 구조는 급락 직전 고점 ≥ +30% 를 요구하므로, 이 모집단은 신호 이전 정보로 이미 충족되는 선택이다 (미래 정보 선택 아님).
분봉: market.db 와 research.db 에서 (종목, 거래일 창[전날 20:00 ET ~ 당일 20:00 ET)) 을 읽어 봉이 더 많은 쪽 사용. 세션·거래일은 ET 시각으로 직접 판정(research.db 에는 열이 없음).
날 선택: 그 날 모집단의 1분봉 보유율 ≥90% 인 날만. 정규장 완전성(정규장 봉 거래량 / 일봉 거래량 0.7~1.3배)도 같이 기록."""
import pickle, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np
ET, UTC = ZoneInfo("America/New_York"), timezone.utc
OUT = "D:/pennybot-live/state/research/cr_other_days.pkl"
GAIN = 0.30
mk = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True); rs = sqlite3.connect("file:D:/pennybot-live/state/research.db?mode=ro", uri=True)
daily = defaultdict(list)
for s, d, h, c, v in mk.execute("SELECT symbol, d, high, close, volume FROM daily_bars WHERE d >= '2026-05-15' AND close > 0 ORDER BY symbol, d"):
    daily[s].append((d, h, c, v or 0))
RUN = defaultdict(list)
for s, x in daily.items():
    for i in range(21, len(x)):
        pc = x[i - 1][2]; adv = np.mean([z[2] * z[3] for z in x[i - 21:i - 1]])
        d = x[i][0]
        if 0.05 <= pc <= 10 and x[i][1] >= (1 + GAIN) * pc and adv >= 25e3 and "2026-07-01" <= d <= "2026-11-13" and not ("2026-10-20" <= d <= "2026-11-11"):
            RUN[d].append((s, float(pc), float(adv), float(x[i][3]), float(x[i][1] / pc - 1)))
def ses_of(dt):                                                       # ET 시각 → 세션
    h = dt.hour + dt.minute / 60
    return "DAY" if (h >= 20 or h < 4) else "PRE" if h < 9.5 else "REGULAR" if h < 16 else "AFTER"
def get_bars(sym, td):
    st = datetime.fromisoformat(td).replace(tzinfo=ET) - timedelta(hours=4)
    a, b = st.astimezone(UTC).isoformat(), (st + timedelta(hours=24)).astimezone(UTC).isoformat()
    best = []
    for con in (rs, mk):
        r = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND ts >= ? AND ts < ? ORDER BY ts", (sym, a, b)).fetchall()
        if len(r) > len(best): best = r
    return best
days_out, rows = {}, []
for td in sorted(RUN):
    syms = RUN[td]; got = []
    for s, pc, adv, dvol, gain in syms:
        r = get_bars(s, td)
        if len(r) < 30: continue
        ts = np.array([datetime.fromisoformat(x[0]).timestamp() for x in r]); a = np.array([x[1:] for x in r], float)
        ses = [ses_of(datetime.fromtimestamp(t, ET)) for t in ts]
        regv = float(sum(v for (v, s_) in zip(a[:, 4], ses) if s_ == "REGULAR"))
        got.append(dict(sym=s, td=td, pc=pc, adv=adv, day_gain=gain, ts=ts, o=a[:, 0], h=a[:, 1], l=a[:, 2], c=a[:, 3], v=a[:, 4], ses=ses, ratio=regv / dvol if dvol > 0 else 0.0))
    rows.append((td, len(syms), len(got)))
    days_out[td] = got
good = [td for td, n, g in rows if n >= 3 and g / n >= 0.90]
print(f"후보 날 {len(rows)} · 모집단(일봉 고가 +30%↑) 합 {sum(n for _, n, _ in rows)} · 분봉 있음 {sum(g for _, _, g in rows)} ({sum(g for _,_,g in rows)/sum(n for _,n,_ in rows)*100:.0f}%)")
bym = defaultdict(lambda: [0, 0, 0])
for td, n, g in rows: bym[td[:7]][0] += n; bym[td[:7]][1] += g; bym[td[:7]][2] += 1
print({m: f"{v[1]}/{v[0]} ({v[1]/v[0]*100:.0f}%) {v[2]}일" for m, v in sorted(bym.items())})
print("커버리지 ≥90% 인 날:", len(good), "·", good[:1], "~", good[-1:], "· 제외된 날:", [(td, f"{g/n*100:.0f}%") for td, n, g in rows if td not in good][:10])
sym_days = [x for td in good for x in days_out[td]]
rat = [x["ratio"] for x in sym_days]
print(f"선택된 날의 종목-날 {len(sym_days)} · 정규장 완전성(정규장 1분봉 거래량/일봉 거래량) 중앙 {np.median(rat):.2f} · 0.7~1.3배 비율 {np.mean([0.7 <= r <= 1.3 for r in rat])*100:.0f}%")
pickle.dump(dict(days=good, rows=rows, sym_days=sym_days), open(OUT, "wb"))
