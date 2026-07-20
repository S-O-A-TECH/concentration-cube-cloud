# Alibaba Cloud ECS 배포 가이드

운영 서버(8100)와 자가진화 서버(8200)를 **ECS 한 대에서 함께** 띄우는 번들이다.
로컬 개발용 `server/local/docker-compose.yml` 은 건드리지 않는다 — 이 디렉터리 파일만 쓴다.

| 파일 | 역할 |
|---|---|
| `docker-compose.cloud.yml` | 6개 서비스(postgres·redis·api·worker·scheduler·evolution) 정의 |
| `.env.cloud.example` | 필요한 환경변수 전체 목록 (→ `.env` 로 복사해 사용) |
| `bootstrap.sh` | 새 ECS 인스턴스 초기 세팅 + 기동 (몇 번 실행해도 안전) |
| `verify_deployment.sh` | 배포 검증 4종 — 해커톤 배포 증빙으로도 사용 |

---

## 1. ECS 인스턴스 사양

| 항목 | 권장 값 | 비고 |
|---|---|---|
| 리전 | **Singapore (ap-southeast-1)** | Qwen 국제 엔드포인트와 같은 리전 = 지연 최소 |
| 인스턴스 | `ecs.e-c1m2.large` (2 vCPU / 4 GB) 이상 | 최소 사양. 컨테이너 6개 + 빌드가 돌아간다 |
| 아키텍처 | x86_64 | ARM64(`ecs.c6r`)도 동작한다 — 이미지가 멀티아키다 |
| OS | **Ubuntu 24.04 LTS 64bit** | |
| 시스템 디스크 | 40 GB 이상 (ESSD) | 기본 20 GB 는 이미지 빌드에 빠듯하다 |
| 공인 IP | 할당 (또는 EIP 바인딩) | 없으면 외부에서 접근 불가 |
| 대역폭 | 종량제 1~5 Mbps | 데모 트래픽에 충분 |

> **메모리 주의**: 2 GB 인스턴스에서는 pandas/pyarrow 설치 중 빌드가 OOM 으로 죽는다. 4 GB 를 쓴다.

## 2. 보안그룹 규칙

**인바운드 — 아래 3개만 연다.**

| 포트 | 프로토콜 | 소스 | 용도 |
|---|---|---|---|
| 22 | TCP | **내 IP 만** (`x.x.x.x/32`) | SSH |
| 8100 | TCP | `0.0.0.0/0` (또는 심사용 IP) | 운영 서버 API |
| 8200 | TCP | `0.0.0.0/0` (또는 심사용 IP) | 자가진화 서버 UI |

**절대 열지 말 것 — PostgreSQL(5432) · Redis(6379).**
`docker-compose.cloud.yml` 은 이 둘에 호스트 포트를 아예 발행하지 않으므로 보안그룹에서 열어도 도달하지 않지만,
규칙 자체를 만들지 않는 것이 안전하다. 특히 **인증 없는 Redis 를 인터넷에 노출하면 즉시 크립토마이너에 감염된다.**

DB 를 직접 봐야 하면 포트를 열지 말고 SSH 터널을 쓴다:
```bash
ssh -L 5433:localhost:5433 ubuntu@<EIP>
```
(터널을 쓰려면 compose 의 postgres 에 `ports: ["127.0.0.1:5433:5432"]` 를 임시로 추가해야 한다.)

## 3. 배포 절차

### 3-1. 코드 올리기

ECS 에 SSH 접속 후, 저장소를 가져온다.

```bash
ssh ubuntu@<EIP>

# 방법 A) git 저장소가 있는 경우 — bootstrap.sh 가 clone 까지 해 준다
curl -fsSLO https://raw.githubusercontent.com/<org>/<repo>/main/deploy/alibaba/bootstrap.sh
chmod +x bootstrap.sh
REPO_URL=https://github.com/<org>/<repo>.git ./bootstrap.sh
```

```powershell
# 방법 B) 로컬 PC 에서 직접 업로드 (git 없이) — Windows PowerShell 기준
scp -r .\concentration_app ubuntu@<EIP>:~/concentration_app
```
업로드 방식이면 ECS 에서:
```bash
cd ~/concentration_app/deploy/alibaba
chmod +x bootstrap.sh verify_deployment.sh
./bootstrap.sh
```

### 3-2. 첫 실행 — `.env` 생성

`bootstrap.sh` 는 처음 실행 시 `.env` 를 만들고 **종료코드 2 로 멈춘다.** 정상 동작이다.

```bash
nano .env      # change-me-* 를 전부 실제 값으로 교체
```

강한 값 생성:
```bash
openssl rand -hex 24
```

반드시 확인할 것:
- `QWEN_API_KEY` / `QWEN_BASE_URL` / `QWEN_MODEL` — **세 값이 한 세트**여야 한다.
  키가 발급된 워크스페이스의 URL 과 그 워크스페이스에서 쓸 수 있는 모델 id 를 넣는다.
  로컬 `live-evolution-server/local/.env` 의 값을 그대로 복사하는 것이 가장 안전하다.
- `EVOLUTION_TOKEN` — 두 서버가 공유한다. 한쪽만 바꾸면 연동이 인증 실패로 끊긴다.

### 3-3. 다시 실행

```bash
./bootstrap.sh
```

`.env` 에 `change-me` 가 남아 있으면 기동을 거부한다(약한 시크릿 노출 방지).
빌드 → 기동 → health 대기까지 진행하고, 마지막에 접속 주소를 출력한다.
최초 실행은 이미지 빌드 때문에 **5~10분** 걸린다.

### 3-4. 검증

```bash
./verify_deployment.sh                        # ECS 안에서
./verify_deployment.sh http://<EIP>           # 외부(내 PC)에서
./verify_deployment.sh http://<EIP> | tee proof.txt   # 증빙 파일로 저장
```

4가지를 검사하고 항목별 PASS/FAIL 을 찍는다.

1. 운영 서버 `GET /health`
2. 자가진화 서버 `GET /api/health`
3. 자가진화 → 운영 서버 내부 연동 (컨테이너 네트워크 `http://api:8100`)
4. **Qwen 실호출** — 배포된 API 를 통해 Model Studio 에 실제 요청 1건

4번이 이 번들의 핵심 증빙이다. `POST /api/agents/qwen/check` 가 어댑터의 `check_auth()` 를 태우고,
그 안에서 `chat/completions` 로 초소형 실요청이 나간다. 모킹이 아니다.

### 3-5. 브라우저 확인

- 자가진화 서버 UI: `http://<EIP>:8200` → `.env` 의 `EVO_ADMIN_ID` / `EVO_ADMIN_PW` 로 로그인
- 운영 서버 health: `http://<EIP>:8100/health`

---

## 4. 일상 운영 명령

```bash
cd ~/concentration_app/deploy/alibaba
alias dc='docker compose -f docker-compose.cloud.yml'

dc ps                          # 상태
dc logs -f api evolution       # 실시간 로그
dc logs --tail=100 worker      # 채점 워커 로그
dc restart evolution           # 한 서비스만 재시작
dc up -d --build               # 코드 갱신 후 재배포
dc down                        # 정지 (데이터 볼륨은 유지)
dc down -v                     # 정지 + 데이터 전부 삭제 — 주의
```

`.env` 를 고쳤으면 `dc up -d` 를 다시 돌려야 컨테이너에 반영된다.

---

## 5. 문제 해결

### `required variable ... is missing a value`
`.env` 에 해당 변수가 없다. 이 compose 는 시크릿에 기본값을 주지 않는다(의도된 설계).
`.env.cloud.example` 과 대조해 빠진 항목을 채운다. `--env-file .env` 를 빠뜨린 경우도 같은 증상이다.

### `.env 에 change-me 자리표시자가 남아 있습니다`
`bootstrap.sh` 가 약한 시크릿으로 배포되는 것을 막은 것이다. 출력된 줄 번호의 값을 교체한다.

### 외부에서 8100/8200 에 접속이 안 된다
순서대로 확인한다.
1. ECS 안에서는 되는가 — `curl http://127.0.0.1:8100/health`
   - 된다면 네트워크 문제 → 2번으로. 안 된다면 컨테이너 문제 → `dc logs api`
2. 보안그룹 인바운드에 8100/8200 이 있는가 (가장 흔한 원인)
3. 인스턴스에 공인 IP/EIP 가 붙어 있는가
4. Ubuntu 방화벽 — `sudo ufw status` (기본 inactive 라면 문제 없음)

### api 컨테이너가 계속 재시작한다
대부분 DB 마이그레이션 실패다.
```bash
dc logs api | tail -50
dc ps postgres            # healthy 인지
```
`POSTGRES_PASSWORD` 를 나중에 바꿨다면 기존 볼륨의 비밀번호와 어긋난 것이다.
데이터를 버려도 되면 `dc down -v && dc up -d`.

### verify 4번(Qwen)만 FAIL
`auth_detail` 메시지를 보고 판단한다.
- **인증 실패(401/403)** — 키가 틀렸거나, 키와 `QWEN_BASE_URL` 의 워크스페이스가 다르다.
- **HTTP 404 / model not found** — 그 워크스페이스에 없는 `QWEN_MODEL` 이다. 모델 id 는 소문자만 허용된다.
- **응답 없음** — ECS 아웃바운드가 막혔다. 보안그룹 아웃바운드는 기본 전체 허용이니 드물다.
  `curl -sS -o /dev/null -w '%{http_code}\n' <QWEN_BASE_URL>/chat/completions` 로 도달성만 확인.

키를 고쳤다면 `dc up -d evolution` 으로 재기동한다.
어댑터가 인증 결과를 **30분 캐시**하므로, 재기동 없이 다시 검사하면 옛 결과가 보일 수 있다.

### verify 3번(연동)만 FAIL
`EVOLUTION_TOKEN` 이 두 서비스에서 다르게 주입된 경우다. compose 는 같은 변수를 양쪽에 넣으므로
`.env` 를 고친 뒤 `dc up -d` 를 안 돌린 상황이 대부분이다.

### 진화 서버 화면에 "기기 연결 안 됨"
정상이다. 웹캠 프로토(8123)는 ECS 에 없다. 대시보드의 device 항목만 `ok:false` 로 뜨고
나머지 기능은 영향받지 않는다.

### 디스크가 찼다
```bash
docker system df
docker system prune -a --volumes    # 주의: 미사용 볼륨까지 삭제
```

---

## 6. 알아 둘 설계 사항

- **데이터 지속성** — 이름 있는 볼륨 3개에 담긴다.
  `pgdata`(PostgreSQL), `storage`(세션 파일), `evostate`(진화 서버의 `state.sqlite`·`secret.key`·`workspaces`).
  `dc down` 은 볼륨을 지우지 않는다. `dc down -v` 만 지운다.
- **자가진화 서버 컨테이너화** — `live-evolution-server/local/Dockerfile` 을 이번에 새로 추가했다.
  기존 `run.py` 는 `127.0.0.1` 고정 바인딩(로컬 전용 설계)이라 컨테이너에서 못 쓴다.
  이미지는 `uvicorn` 을 직접 띄우고, 노출 범위는 compose 포트 매핑과 보안그룹이 통제한다.
- **웹캠 프로토 디렉터리 불필요** — `WEBCAM_ROOT` 는 `config.py` 가 값을 보관만 하고
  런타임 코드가 읽지 않는다(참조는 `tests/test_sim_sync.py` 뿐이며 경로가 없으면 skip).
  그래서 마운트하지 않고 없는 경로를 넣어 둔다.
- **멀티아키** — 두 이미지 모두 `python:3.12-slim` 기반이고 x86 전용 바이너리가 없다.
  pandas/numpy/pyarrow 는 amd64·aarch64 모두 휠이 제공되어 ARM 인스턴스에서도 그대로 뜬다.
- **HTTPS 없음** — 8100/8200 은 평문 HTTP 다. 해커톤 데모 범위로는 충분하지만
  실서비스로 가면 Caddy/Nginx 로 TLS 를 종단해야 한다
  (`server/local/docker-compose.yml` 에 `caddy` 서비스 자리가 예약되어 있다).
