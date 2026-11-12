"""대시보드 설정 탭 ↔ 수집기: state/settings.json (켜기·끄기, 가상 계좌 금액). 수집기는 바뀌면 바로 다시 읽는다."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "strategies": {"S1": True, "S2": True, "S3": True, "S4": True, "S5": True, "S6": True},
    "alerts": {"presurge": True, "burst": True, "early20": True, "after_move": True, "bottom_bounce": True, "steady": False, "halt": True, "pre_open": True, "vtrade": True, "dm_pick": True, "briefing": True, "perf": True, "reaccel": True, "approach": True, "early_watch": True, "absorb": True, "dna": True, "fast": True, "miss": True, "spike_start": True, "day_open": True, "slow_climb": True, "ignition": True},
    "realtime": True,
    # 11/12: 전 종목 훑기로 잡은 뒤 2분 안 웹소켓 대금 기준 ($, 세션별) · 감시 목록 시총 상한 · 20일 평균 거래대금 하한 (0 = 끔)
    "sweep_min_dollar": {"DAY": 1000, "PRE": 3000, "REGULAR": 5000, "AFTER": 5000},
    "universe_max_mcap": 500000000,
    "universe_min_adv": 25000,
    "vaccount": {"capital": 10000, "per_trade": 1000},
}
ALERT_KO = {"presurge": "🚨 급등 전조", "burst": "📶 거래 터짐 신호", "early20": "🌙 정규장 전 +20% 돌파", "after_move": "🌆 애프터마켓 누적 변동",
            "bottom_bounce": "🌊 바닥 찍고 재반등 (2차 파동 초입)",
            "steady": "🫧 꾸준히 거래 붙음 (사부작 지속 관심)",
            "halt": "🛑 뉴스 대기 거래정지", "pre_open": "🌅 프리 개장 주목 목록",
            "vtrade": "🧪 가상 매매 (주문·체결·매도)", "dm_pick": "🌞 데이마켓 후보 TOP 12 (한국 낮 12시)", "briefing": "🗞 장 시작 전 브리핑 (03:45 ET)", "perf": "📊 장 마감 알림 성과 요약", "reaccel": "🔁 재가속 (급등 뒤 눌렸다 다시 붙음)", "approach": "👀🎯 이전 고점 접근·임박 (며칠 전 급등주, 넘기 전)",
            "early_watch": "👀 초기 경보 (10분 +3%, 점수 높음·공시 뒤 · 매수 없음)",
            "absorb": "🧲 물량 흡수 (매수호가에서 나오는 매도를 다 받아 줌 · 검증 전)",
            "dna": "🧬 급등 이력 감시 정리 (장 끝나고 하루 한 번)",
            "fast": "⚡ 빠른 알림 (첫 거래량 터지는 60초 안 · 정규장 · 소형주 · 매수 신호 아님)",
            "miss": "📝 놓친 급등주 복기 (장 끝나고 하루 한 번 · +30% 간 종목을 빨리·늦게·놓침으로)",
            "spike_start": "🚀 급등 시작 (1분새 +4%, 아직 전날 대비 +10% 전 · 거래 붙음)",
            "day_open": "🌙 데이마켓 개장 갭 상승 목록 (20:30 ET 한 번 · 추격 금지)",
            "slow_climb": "🐢 천천히 오름 (1분 급등 없이 +20% 넘음 · 오늘 대금 $100만↑ · 감시용)",
            "ignition": "🔥 데이·프리 점화 (저점 +10% · 1분봉 +2.8% · $2 미만 · 그날 +50% 더 감 10% · 매수 신호 아님)"}


def path(state_dir: str) -> Path:
    return Path(state_dir) / "settings.json"


def load(state_dir: str) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    try:
        got = json.loads(path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for k, v in got.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update({kk: vv for kk, vv in v.items() if kk in out[k]})
        elif k in out:
            out[k] = v
    return out


def save(state_dir: str, patch: dict) -> dict:
    cur = load(state_dir)
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
            for kk, vv in v.items():
                if kk in DEFAULTS[k]:
                    cur[k][kk] = type(DEFAULTS[k][kk])(vv)
        elif not isinstance(DEFAULTS[k], dict):
            cur[k] = type(DEFAULTS[k])(v)
    p = path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return cur


class Watcher:
    """파일이 바뀌었을 때만 다시 읽는다."""

    def __init__(self, state_dir: str):
        self.state_dir, self._mtime, self.value = state_dir, None, load(state_dir)

    def get(self) -> dict:
        try:
            m = path(self.state_dir).stat().st_mtime
        except OSError:
            m = None
        if m != self._mtime:
            self._mtime, self.value = m, load(self.state_dir)
        return self.value
