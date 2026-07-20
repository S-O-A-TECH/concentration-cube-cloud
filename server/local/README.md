# concentration-cube 운영 서버 (local 구현)

계획 문서: [`../docs/plan/`](../docs/plan/README.md) (spec / planning / check-list).
**현재 구현 단계: S0~S7 로컬 완성 (S8 클라우드 이전만 남음).**

| 영역 | 내용 |
|:---|:---|
| 세션 수집 (S2) | `POST /v1/devices/auth`(JWT 1h) → `start` → `chunk×N`(CRC32) → `finish`. 전 세션 API 는 `X-Nonce`+`X-Timestamp`(5분 창) 필수. CRC 규약: `app/services/integrity.py` (기기측 공유 — `tests/device_sim.py`) |
| 채점 (S3) | finish → RQ 잡 자동 채점(활성 param_set 매회 조회) → `scoring_runs`(INSERT only) + `promoted_results`. focus_scoring 정본은 여기 — 프로토 동기화: `tools/sync_focus_scoring.py`. LLM 코칭: Qwen — Alibaba Cloud Model Studio(수치 요약만, `.env` 키) |
| 프로토 연결 (S4) | 웹캠 프로토가 자동 업로드(오프라인 pending·재시도). 일치 검증: `web_cam_version_prototype/tools/verify_server_match.py` |
| Evolution (S5) | `/v1/evolution/*` — 라벨(불변)·오답노트·param_set 세대·evaluate 성적표(게이트 서버 계산)·promote(confirm)·rollback·jobs. 인증: `X-Evolution-Token` |
| 운영 콘솔 (S6) | `/ops` (로그인 admin — `.env`) — 대시보드·계정·소비자·기기(토큰 발급)·세션·리포트 뷰·감사. API: `/v1/ops/*` (쿠키). evolution 과 완전 분리 |
| 보안·운영 (S7) | 로그인 잠금(5회/10분), 삭제권 잡(잔존 0), 정리 잡(scheduler 서비스), 백업 `scripts/backup.ps1`, 복구 `RUNBOOK_복구.md`, 부하 스모크 `tools/s7_load_smoke.py` |

E2E 도구: `tools/s2_dod_upload.py`(업로드) · `tools/s3_dod_e2e.py`(자동 채점) · `tools/verify_restore.py`(복구 검증).

### S8(클라우드 이전) 전 반드시 닫을 항목 — 2026-07-05 코드리뷰 잔여 (전부 다중워커/프록시 조건부)

- [ ] `.env` `REQUIRE_SECURE_SECRETS=true` 켜기 (기본 시크릿이면 부팅 거부 — main.py `_check_secrets`)
- [ ] worker 를 2개 이상으로 늘리기 전: `promoted_results` 갱신을 upsert(ON CONFLICT)로 강화 (`jobs/score_session.py` R1 주석)
- [ ] promote/rollback 완전 직렬화가 필요하면 PG advisory lock 도입 (현재는 진행 중 재채점 잡 409 가드 — 서로 다른 세트 동시 promote 의 극단 경합만 잔존)
- [ ] Caddy 리버스프록시 뒤에서는 ops 로그인 잠금 키의 `request.client.host` 가 프록시 IP — X-Forwarded-For 신뢰 처리 필요 (`api/ops.py`)
- [ ] Redis 장기 아웃티지 시 nonce 폴백 재시도가 요청마다 2s 커넥트 시도 — 필요 시 네거티브 캐시 추가 (`api/deps.py`)

포트 배치 (프로젝트 전체 규칙):

| 시스템 | 주소 |
|:---|:---|
| **이 서버 (운영)** | `http://127.0.0.1:8100` |
| 웹캠 프로토 | `http://127.0.0.1:8123` |
| live-evolution 서버 | `http://127.0.0.1:8200` |
| postgres (컨테이너) | `127.0.0.1:5433` (호스트 직접 실행 모드용 노출) |
| redis (컨테이너) | `127.0.0.1:6380` (6379 는 이 PC 의 WSL 내부 redis 가 점유) |

## 실행법 ① — Docker Compose 전체 (표준)

```powershell
cd server\local
copy .env.example .env      # 최초 1회 — 시크릿 실값으로 교체
docker compose up -d --build
# 확인
curl http://127.0.0.1:8100/health
# → {"ok":true,"db":true,"redis":true,"storage":true,"version":"dev"}
```

종료/재시작 시 DB 데이터는 named volume(`pgdata`)에 유지된다: `docker compose down && docker compose up -d`.

## 실행법 ② — 호스트 직접 실행 (디버깅용)

api 만 호스트에서 `--reload` 로 띄우고, DB/redis 는 컨테이너를 쓴다.

```powershell
cd server\local
docker compose up -d postgres redis    # DB·redis 컨테이너만

python -m venv .venv                   # 최초 1회
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
```

`.env` 기본값이 이 모드 기준(`127.0.0.1:5433` / `127.0.0.1:6379`)으로 맞춰져 있다.

## DB 마이그레이션·시드 (S1)

컨테이너 기동 시 api 가 `alembic upgrade head` + 멱등 시드를 자동 실행한다. 수동 실행:

```powershell
$env:PYTHONUTF8='1'                                   # Windows cp949 방지
.venv\Scripts\python -m alembic upgrade head          # 최신으로
.venv\Scripts\python -m alembic downgrade base        # 전부 롤백
.venv\Scripts\python -m alembic revision --autogenerate -m "..."   # 모델 변경 후 새 리비전
.venv\Scripts\python -m app.db.seed                   # 시드 (재실행 안전)
```

시드가 만드는 것: `param_sets` v1.0(adopted, 웹캠 프로토 default.json) + `WEBCAM_PROTO_001` 기기.
기기 factory_token 평문은 **생성 시 1회만 출력** — 분실 시 해시 재발급.

## 테스트

```powershell
.venv\Scripts\python -m pytest tests/ -q            # 전체
.venv\Scripts\python -m pytest tests/test_health.py::test_health_all_green -q   # 1건
```

DB/redis 컨테이너 없이도 통과하도록 연결 체크는 테스트에서 monkeypatch 된다.

## 구조 (SPEC-00 §4)

```
app/
├─ main.py            # FastAPI 앱 조립
├─ config.py          # .env 설정 (pydantic-settings)
├─ api/               # health(S0) → devices·sessions(S2) → results(S3) → evolution(S5) → ops(S6)
├─ db/                # S1: SQLAlchemy 모델 + Alembic
├─ services/          # S2~: 저장소 추상화·무결성·승격
├─ jobs/              # S3: RQ 잡 (큐 이름 scoring)
├─ ops_ui/            # S6: 운영 관리자 페이지 (/ops)
└─ focus_scoring/     # S3: 채점 엔진 정본 (웹캠 프로토에서 이식 — SPEC-03)
```

## 원칙 (docs/plan/README.md)

1. **채점은 순수 함수** — 같은 parquet + 같은 param_set = 언제나 같은 result.
2. **원본 불변** — 세션 parquet 업로드 후 수정 금지. 재채점은 새 scoring_run.
3. **영상 무수신** — 영상 수신 경로 자체가 없다 (multipart/영상 MIME 거부).
