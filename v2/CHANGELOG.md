# pennybot2 변경 기록

`v2/` (처음부터 다시 만드는 새 버전)의 변경만 적는 파일입니다. 기존 버전의 기록은 루트 `CHANGELOG.md` 에 따로 있습니다.
동작이 바뀌면 같은 커밋에서 날짜와 함께 여기에 적습니다. 계획은 `PLAN.md`.

## 2026-11-25

- 새로 만들기 시작. 1단계 뼈대: 설정(`config.py`), 세션 캘린더(`sessions.py`), SQLite 저장소(`store.py`), 알림 전송(`notify.py`), 테스트(`tests2/`).
- 폴더 구조 정리: 새 버전 전부를 `v2/` 안으로 (`v2/pennybot2/core`, `v2/pennybot2/alerts`, `v2/tests`, `v2/PLAN.md`, `v2/CHANGELOG.md`). 기존 버전 파일과 섞이지 않음.

## 2026-11-25 (구조 재설계)

- **이벤트 흐름 구조로 다시 설계**: 입력(feeds) → 이벤트 버스 → 감지기(detectors) → 출력(sinks). 기존 `alerts/` 는 `sinks/` 로.
- 새로 추가: `core/events.py`(Tick·Bar·Signal), `core/bus.py`(구독자 실패 격리), `feeds/replay.py`(저장 봉 재생), `detectors/surge.py`(급등: 5분 안 +10%, 종목별 15분 쿨다운), `sinks/console.py`, `sinks/notify.py`, `app.py`(연결만), `python -m pennybot2 replay`.
