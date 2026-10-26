"""대시보드 설정 탭 ↔ 수집기: state/settings.json (켜기·끄기, 가상 계좌 금액). 수집기는 바뀌면 바로 다시 읽는다."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "strategies": {"S1": True, "S2": True, "S3": True, "S4": True, "S5": True, "S6": True},
    "alerts": {"presurge": True, "burst": True, "early20": True, "after_move": True, "halt": True, "pre_open": True, "vtrade": True, "briefing": True, "perf": True, "reaccel": True, "approach": True},
    "realtime": True,
    "vaccount": {"capital": 10000, "per_trade": 1000},
}
ALERT_KO = {"presurge": "🚨 급등 전조", "burst": "📶 거래 터짐 신호", "early20": "🌙 정규장 전 +20% 돌파", "after_move": "🌆 애프터마켓 누적 변동",
            "halt": "🛑 뉴스 대기 거래정지", "pre_open": "🌅 프리 개장 주목 목록",
            "vtrade": "🧪 가상 매매 (주문·체결·매도)", "briefing": "🗞 장 시작 전 브리핑 (03:45 ET)", "perf": "📊 장 마감 알림 성과 요약", "reaccel": "🔁 재가속 (급등 뒤 눌렸다 다시 붙음)", "approach": "👀🎯 이전 고점 접근·임박 (며칠 전 급등주, 넘기 전)"}


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
