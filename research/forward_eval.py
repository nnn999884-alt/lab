"""앞으로 쌓이는 거래일로만 하는 최종 검증 (규칙은 frozen_rules.json 에 동결, 코드 해시 확인). 사용: python forward_eval.py [--dry 시작일]
동결 규칙 P1 = 거래량 첫 폭발 트리거(early_study.py 정의 그대로) ∧ 직전 30분 폭 ≥ 20% → 진입 = 다음 봉 '매도1호가'(호가 확인된 것만 체결 인정),
청산 = 3×ATR 손절 / 2R 익절 / 120분 (early_exits.py S2), 실제 청산가 = min(봉 시뮬레이션가, 청산 시각 매수1호가). 포지션 = 계좌의 min(1%/손절폭, 25%), 수수료 0.2%.
호가 없는 사건은 '체결 미확인' 으로 따로 집계 (성과에 포함 안 함). 판정 기준은 frozen_rules.json 에 미리 적어 둠."""
import hashlib, json, pickle, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
sys.path.insert(0, "D:/pennybot-live/research")
import early_study as ES
RULES = Path("D:/pennybot-live/research/frozen_rules.json")
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def quote_before(sym, t, win=180):
    iso, lo = datetime.fromtimestamp(t, timezone.utc).isoformat(), datetime.fromtimestamp(t - win, timezone.utc).isoformat()
    return con.execute("SELECT bid, bid_size, ask, ask_size FROM quotes WHERE symbol=? AND ts <= ? AND ts >= ? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (sym, iso, lo)).fetchone()
def s2_trade(ts, o, h, l, c, k, e):
    atr = np.mean((h[max(0, k - 14):k + 1] - l[max(0, k - 14):k + 1]) / c[max(0, k - 14):k + 1]); d = min(max(3 * atr, 0.03), 0.25)
    stop, tp, j0 = e * (1 - d), e * (1 + 2 * d), k + 1
    for j in range(j0, len(c)):
        if ts[j] > ts[j0] + 7200: return c[j - 1], ts[j - 1], d
        if j > j0 and o[j] <= stop: return o[j], ts[j], d
        if l[j] <= stop: return stop, ts[j], d
        if h[j] >= tp: return tp, ts[j], d
    return c[-1], ts[-1], d
def main(since=None, dry=False):
    fr = json.loads(RULES.read_text(encoding="utf-8"))
    for fn, hv in fr["code_sha256"].items():
        if sha(f"D:/pennybot-live/research/{fn}") != hv: sys.exit(f"⚠ {fn} 이 동결 뒤 바뀜 — 검증 무효 (규칙을 바꾸려면 새 동결과 새 검증 기간이 필요)")
    since = since or fr["forward_from"]
    ev = ES.run(since, "2099-12-31", out_name="early_events_forward.pkl", min_syms=fr["min_symbols_per_day"])
    sub = [e for e in ev if e["feats"]["range30"] >= fr["range30_min"]]
    rows, unconf = [], 0
    for e in sub:
        qs = quote_before(e["sym"], e["entry_ts"])
        if not qs: unconf += 1; continue
        b = con.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
        ts = np.array([datetime.fromisoformat(r[0]).timestamp() for r in b]); a = np.array([r[1:] for r in b], float)
        o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]; k = int(np.argmin(np.abs(ts - e["ts_trig"])))
        ent = qs[2]
        px, tx, d = s2_trade(ts, o, h, l, c, k, o[k + 1]); qx = quote_before(e["sym"], tx)
        if not qx: unconf += 1; continue
        ex = min(px, qx[0]); pos = min(0.01 / d, 0.25)
        rows.append(dict(td=e["td"], ses=e["ses"], sym=e["sym"], ret=ex / ent - 1 - 0.002, pnl=pos * (ex / ent - 1 - 0.002), d=d))
    tag = "※ 시험 실행(dry) — 이 기간은 탐색에 이미 쓰인 날이라 검증 결과가 아님" if dry else "최종 검증"
    print(f"[{tag}] 기간 {since}~ · 트리거 {len(ev)} · 조건 충족 {len(sub)} · 호가 확인 체결 {len(rows)} · 체결 미확인(제외) {unconf}")
    if not rows: print("호가가 확인된 거래 없음"); return
    days = sorted({r["td"] for r in rows}); by = {d: [r["pnl"] for r in rows if r["td"] == d] for d in days}
    rng = np.random.default_rng(0); bs = [np.mean(np.concatenate([by[d] for d in rng.choice(days, len(days))])) for _ in range(2000)]
    m = np.mean([r["pnl"] for r in rows]); print(f"  거래 {len(rows)} · 거래일 {len(days)} · 계좌 평균 손익/거래 {m*100:+.3f}% · 승률 {np.mean([r['ret']>0 for r in rows])*100:.0f}% · 날짜 부트스트랩 95% [{np.percentile(bs,2.5)*100:+.3f}%, {np.percentile(bs,97.5)*100:+.3f}%]")
    ok = len(rows) >= fr["criteria"]["min_trades"] and len(days) >= fr["criteria"]["min_days"] and np.percentile(bs, 2.5) > 0
    print("  판정(사전 기준):", "통과 — 모의매매 단계로" if ok and not dry else ("기준 미달/표본 부족 또는 시험 실행" if True else ""))
if __name__ == "__main__":
    dry = "--dry" in sys.argv
    main(sys.argv[sys.argv.index("--dry") + 1] if dry else None, dry)
