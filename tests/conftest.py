import pytest

import test_trading

env = test_trading.env      # test_trading 의 fixture 를 다른 테스트 파일에서도 쓴다


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """테스트마다 빈 임시 폴더에서 실행 — 기본 경로(state/market.db 등)로 만든 수집기가
    실제 state/ 의 collector_status.json · briefing.json · rank_model.pkl 을 덮어쓰지 않게 (2026-11-08 사고)."""
    import pennybot
    from pathlib import Path
    root = Path(pennybot.__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.chdir(tmp_path)
