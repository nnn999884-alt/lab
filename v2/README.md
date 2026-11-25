# pennybot2 (새 버전)

루트의 `pennybot/` 은 기존 버전이고, 이 `v2/` 폴더가 처음부터 다시 설계한 새 버전입니다. 서로 코드·테스트·기록을 공유하지 않습니다.
목표: **실시간 감시·알림** (매매는 모의만).

## 구조: 입력 → 이벤트 버스 → 감지기 → 출력
```
feeds (시세·일봉·공시)  ──Tick/Bar──▶  Bus  ──▶ detectors ──Signal──▶ Bus ──▶ sinks (알림·저장·모의매매)
```
- `pennybot2/core/` 설정 · 세션 캘린더 · 저장소 · 이벤트 · 버스
- `pennybot2/feeds/` 입력. 이벤트만 내보내고 다른 부품을 모른다 (`replay.py` = 저장된 봉 재생 = 백테스트)
- `pennybot2/detectors/` 감지기. 파일 하나 = 감지기 하나 (`surge.py` 급등)
- `pennybot2/sinks/` 출력 (`console.py`, `notify.py` 디스코드·텔레그램)
- `pennybot2/app.py` 부품 연결만 담당 (판단 로직 없음)
- `tests/`, `PLAN.md` 단계별 계획, `CHANGELOG.md` 변경 기록

## 실행
`v2/` 안에서: `python -m pytest -q` (테스트), `python -m pennybot2 replay [종목…]` (저장된 1분봉으로 신호 확인)
