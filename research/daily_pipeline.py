"""새 거래일이 쌓이면 이어서 도는 연구 파이프라인 (독립 연구 코드, 매매·알림 무관).
  python research/daily_pipeline.py [--since 2026-11-16] [--test-days 2026-11-09 --prefix test]

하는 일 (규칙은 사전에 고정 — 이 스크립트는 규칙을 바꾸지 않고 신호만 누적):
 1. 커버리지 ≥2,300종목인 '새 거래일'(처리 안 한 날)만 골라 patterns_lab 으로 패턴 신호 7종(SB 두 번째 거래량 폭발 · PB 눌림 후 재돌파 · VPP 거래량 대비 가격 유지 + HC·SUP·DRV·CR)과 무작위 대조군을 탐지·시뮬레이션 → state/research/pattern_signals_forward.pkl 에 누적.
 2. 패턴별 누적 표본 수(목표 5,000), 총 R 평균(SE), 대조군 대비 차이(t) 를 보고 — 손익 평가가 아니라 '우위가 존재하는지' 표본 누적 검정. 비용 뒤 손익은 별도(patterns_report 방식)이며 수익성이 검증됐다고 주장하지 않는다.
 3. 같은 날의 소급 V0 트리거(early_study, 동결)와 실시간 호가 기록(trigger_quotes)을 맞춰 live_logged / retro_only / live_only 로 분리.
 새 데이터가 충분히 쌓이기 전(패턴당 5,000건 미만)에는 어떤 결론도 '검증됐다'고 쓰지 않는다.
 검증에 쓴 날은 state/research/pipeline_forward.json 에 기록 — 같은 날을 다시 규칙 선택에 쓰지 않도록."""
import argparse, json, pickle, sqlite3, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, "D:/pennybot-live/research")
import patterns_lab as PL
import trigger_join as TJ

OUT = Path("D:/pennybot-live/state/research")
TARGET = 5000
ap = argparse.ArgumentParser()
ap.add_argument("--since", default="2026-11-16")
ap.add_argument("--test-days", nargs="*", default=None)         # 시험 실행: 이 날들만, 결과는 --prefix 파일에 (새 데이터 기록과 섞지 않음)
ap.add_argument("--prefix", default="")
a = ap.parse_args()
tag = (a.prefix + "_") if a.prefix else ""
state_f = OUT / f"{tag}pipeline_forward.json"
sig_f = OUT / f"{tag}pattern_signals_forward.pkl"
state = json.loads(state_f.read_text(encoding="utf-8")) if state_f.exists() else {"processed": []}
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
cov = dict(con.execute("SELECT trade_date, COUNT(DISTINCT symbol) FROM bars_1m WHERE trade_date >= ? GROUP BY 1", (min(a.test_days) if a.test_days else a.since,)))
good = sorted(d for d, n in cov.items() if n >= PL.MIN_SYMS and (a.test_days is None or d in a.test_days) and d >= (min(a.test_days) if a.test_days else a.since))
new = [d for d in good if d not in state["processed"]]
print(f"커버리지 충분한 날 {good} · 새로 처리할 날 {new} · 커버리지 부족으로 대기 중 {[d for d in cov if d not in good]}")
allsig = pickle.load(open(sig_f, "rb")) if sig_f.exists() else []
if new:
    r = PL.run(only_days=set(new), out_name=f"{tag}_tmp_signals.pkl", d_from=min(new))
    allsig += r
    pickle.dump(allsig, open(sig_f, "wb"))
    state["processed"] = sorted(set(state["processed"]) | set(new))
    state_f.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
for s in allsig: s["gR"] = s["gross"] / s["dist"]
g = lambda fam: [s for s in allsig if s["fam"] == fam and s["tp_r"] == 2.0 and s["delay"] == 0]
rnd = np.array([s["gR"] for s in g("RND")]) if g("RND") else np.array([])
print(f"\n[표본 누적 · 총 R 우위 검정] 처리한 새 거래일 {len(state['processed'])}일 ({state['processed'][:1]}~{state['processed'][-1:]}) · 목표 패턴당 {TARGET}건 · 손익(비용 뒤) 평가 아님")
print(f"{'패턴':5s} {'n':>6s} {'진행':>6s} | 총 R 평균 (SE) | 대조군 대비 차 (t)")
for fam in ["SB", "PB", "VPP", "HC", "SUP", "DRV", "CR"]:
    x = np.array([s["gR"] for s in g(fam)])
    if len(x) < 20 or len(rnd) < 20:
        print(f"{fam:5s} {len(x):6d} {len(x)/TARGET*100:5.1f}% | 표본 부족 — 판단 보류"); continue
    se = x.std(ddof=1) / np.sqrt(len(x)); diff = x.mean() - rnd.mean(); sed = np.sqrt(x.var(ddof=1) / len(x) + rnd.var(ddof=1) / len(rnd))
    note = "" if len(x) >= TARGET else "  ← 표본 부족: 결론 금지"
    print(f"{fam:5s} {len(x):6d} {len(x)/TARGET*100:5.1f}% | {x.mean():+.3f} ({se:.3f}) | {diff:+.3f} ({diff/sed:+.1f}){note}")
print(f"(무작위 대조군 n={len(rnd)})")
# 실시간 vs 소급 신호 분리
if good:
    try:
        import early_study as ES
        ev = ES.run(min(good), "2099-12-31", out_name=f"{tag}early_events_forward.pkl", min_syms=PL.MIN_SYMS)
        ev = [e for e in ev if e["td"] in set(good)]
        print("\n[실시간 감지 vs 소급 트리거]"); print(TJ.report(ev, TJ.read_rows(con, min(good))))
    except Exception as e:
        print("트리거 비교 건너뜀:", e)
print("\n※ 위 숫자는 신호 누적·우위 존재 여부만 보여 줍니다. 수익성은 검증되지 않았습니다.")
