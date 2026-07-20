# live-evolution 서버 (local 구현)

> **사용법을 찾는다면** → 각 화면 상단의 **❓ 도움말** 또는 [`GUIDE_사용법.md`](GUIDE_사용법.md)
> (메뉴별 상세 + FAQ). 1세대 리허설 실측 기록은 [`RUNBOOK_진화1세대.md`](RUNBOOK_진화1세대.md).

임상 검증 게이트형 자가진화 엔진의 **조종석** — 정답지(실시간 라벨) → 오답노트 →
AI 에이전트 제안 → 홀드아웃 게이트 → **사람의 [채택] 버튼** → 운영 해석 기준 교체.
설계 정본: `../docs/plan/` (SPEC-00~06, E0~E6).

| 구성요소 | 주소 | 비고 |
|:---|:---|:---|
| **이 서버** | `http://127.0.0.1:8200` | 호스트 직접 실행 (Docker 아님 — AI CLI 의 호스트 OAuth 재사용) |
| 운영 서버 (다른 팀, Docker) | `http://127.0.0.1:8100` | 현재 `/health` 까지 가동 확인 (S5 `/v1/evolution/*` 는 개발 중) |
| **mock 운영 서버** | `http://127.0.0.1:8101` | S5 완성 전까지의 대역 — SPEC-02 §1.3 계약 그대로 |
| 기기 (웹캠 프로토) | `http://127.0.0.1:8123` | `web_cam_version_prototype` — LAB-5(300초) 모드 추가됨 |

## 실행 (PowerShell, 이 폴더에서)

```powershell
# 0) 최초 1회
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 1) mock 운영 서버 (실서버가 8100 을 쓰므로 8101)
.venv\Scripts\python run_mock_ops.py --port 8101

# 2) (선택) 합성 검증 데이터 시드 — 진화 루프를 즉시 돌려보고 싶을 때
.venv\Scripts\python tools\seed_demo_data.py

# 3) 본서버 (로그인 admin/1234 — .env)
.venv\Scripts\python run.py            # → http://127.0.0.1:8200 자동 오픈

# 4) (실물 검증 세션용) 웹캠 프로토도 켜 둔다
#    cd ..\..\web_cam_version_prototype ; .venv\Scripts\python run.py

# 테스트
.venv\Scripts\python -m pytest tests/ -q
```

## 실서버(S5) 전환 — 코드 변경 0

운영 서버 팀의 `/v1/evolution/*` 가 열리면:

1. mock(8101) 종료
2. `.env` 수정: `SERVER_URL=http://127.0.0.1:8100`, `EVOLUTION_TOKEN=<서버 팀과 공유한 값>`
3. 본서버 재시작 — 끝. (전 호출이 `app/server_client.py` 단일 경유이므로 이것으로 충분)

### 운영 서버 팀에 전달할 계약 메모 (mock 이 구현한 de-facto 상세)

SPEC-02 §1.3 에 명시되지 않은 세부를 mock 은 이렇게 정의했다 — S5 구현 시 참고
(어긋나면 문서를 먼저 고치고 양쪽을 맞춘다, E1 리스크 규약):

- `GET sessions/{sid}/detail` 응답에 `classifier_inputs` 포함: 분류기 입력 컬럼의
  per-sample 배열 (`t_ms, face_valid, gaze_valid, gaze_on_page_prob,
  gaze_dispersion_1s, saccade_count_1s`) — Evidence 의 train_lite 재료. 수치뿐(개인정보 0).
- `POST param_sets` 는 `version` 필드를 추가로 받는다 (없으면 서버가 명명).
- 성적표(report)의 `train`/`holdout` 모두 `{sens:{before,after}, spec:{before,after}}`
  구조 (성적표 화면이 양쪽 before→after 를 표로 요구 — SPEC-03 ⑦).
- bin 대조 규칙: 10초 bin, truth=구간이 bin 중앙시각을 덮을 때(웹캠 evaluate.py 와 동일),
  predicted=10Hz 분류 후 bin 다수결(invalid ≥50% → invalid), truth 없음/invalid bin 은
  대조 제외, sens/spec 은 전 세션 합산(pooled). 구현: `app/loop/sim/classify_core.py`.

## 구조

```
app/
├─ main.py            FastAPI 조립 + 화면 라우트 (127.0.0.1:8200)
├─ config.py  db.py  auth.py
├─ server_client.py   운영 서버 /v1/evolution/* 래퍼 — 유일한 경유점
├─ device_client.py   기기 제어 추상화 (v0=웹캠 직결) + 세션시계 보간 (SPEC-05 §3)
├─ console.py         실시간 검증 세션 콘솔 (버튼 4종 → 정답지, 저장 1회·불변)
├─ triggers.py        라벨 누적 알림 / 자동 제안(opt-in, PASSED 정지)
├─ agents/            claude / codex 어댑터 (감지·인증확인·헤드리스 propose)
├─ loop/
│  ├─ machine.py      상태 머신 (SQLite 영속 — 새로고침에도 이어짐)
│  ├─ evidence.py     Evidence Pack 7파일 + train_lite (train 만 — holdout 물리 격리)
│  ├─ workspace.py    작업장 조립 + holdout 부재 검증 + manifest 해시
│  ├─ prompts.py      MISSION.md 렌더 (고정 임무문)
│  ├─ validate.py     4중 검증 (★자가시험 재계산 대조 ±0.5%p)
│  └─ sim/classify_core.py   웹캠 분류 코어 sync 사본 — validate·mock·simulate 공용
└─ webui/             화면 9종 + 설정 (주황 "Live Evolution" 배너, 빌드 없음)

tests/mocks/ops_server.py   기능형 mock — 실물 세션 폴더를 읽고 실제 분류코어로 채점
tools/seed_demo_data.py     합성 LAB-5 세션 + 정답지 시드
tools/backup_state.py       state.sqlite 주간 백업
state/                      런타임 데이터 (sqlite·작업장·백업 — git 제외)
```

## 4원칙 (코드가 강제하는 것)

1. **채택은 언제나 사람** — 자동 모드도 PASSED 에서 정지 (`tests/test_machine.py::test_auto_mode_stops_at_passed`)
2. **이 서버는 판정하지 않는다** — 게이트·승격은 운영 서버(지금은 mock)가 집행, 미통과 promote 는 409
3. **AI 는 도구 실행자** — 4중 검증(스키마→경계→표적→재계산 대조) 통과분만 후보 등록
4. **정답지는 실시간에만** — 콘솔 저장 1회·불변(재-POST 409), 실패는 [연구 제외] 후 재시도

## 알려진 한계 (v0)

- 기기 상태는 0.5s 폴링 (웹캠 WS 와 동일 payload — `DeviceClient` 뒤에 숨겨 교체 가능)
- 콘솔 진행 중 본서버를 재시작하면 그 세션의 클릭 복원은 안 됨 (5분 세션 — [연구 제외] 후 재시도가 설계 답)
- mock 의 게이트 정의는 대역일 뿐 — 실서버 S5 의 게이트가 정본
- [후보 등록] POST 가 성공한 직후·기록 전에 서버가 죽으면(전원 등) 재시작 시 REVIEW_DIFF 로
  복원되어 재등록 시 운영 서버에 고아 후보 1개가 남을 수 있음 — 세대 이력 화면에서 눈으로
  식별 가능(local 기록 없는 후보), 창이 매우 좁아 v0 은 문서화로 갈음
