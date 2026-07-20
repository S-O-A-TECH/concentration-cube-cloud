#!/usr/bin/env bash
# concentration-cube — Alibaba Cloud ECS(Ubuntu 24.04) 부트스트랩.
#
# 멱등: 몇 번을 다시 돌려도 안전하다. 이미 설치된 docker 는 건너뛰고,
#       이미 있는 .env 는 절대 덮어쓰지 않으며, compose 는 변경분만 재생성한다.
#
# 사용법 (ECS 에 SSH 로 접속한 뒤):
#   1) 저장소를 이미 올렸다면:  cd <repo>/deploy/alibaba && ./bootstrap.sh
#   2) git 에서 받아오려면:      REPO_URL=https://github.com/<org>/<repo>.git ./bootstrap.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="docker-compose.cloud.yml"
ENV_FILE=".env"
ENV_EXAMPLE=".env.cloud.example"
CLONE_DIR="${CLONE_DIR:-$HOME/concentration_app}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"   # 초. 최초 빌드는 이미지 다운로드로 오래 걸린다.

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[경고]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[실패]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. 사전 점검 ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] && warn "root 로 실행 중입니다. 일반 사용자(ubuntu)로 실행해도 됩니다."

if ! command -v sudo >/dev/null 2>&1; then
  if [[ $EUID -eq 0 ]]; then sudo() { "$@"; }
  else die "sudo 가 없습니다. root 로 실행하거나 sudo 를 설치하세요."; fi
fi

# ── 1. REPO_URL 이 주어졌으면 저장소 확보 (멱등: 있으면 pull) ──────────────
if [[ -n "${REPO_URL:-}" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    log "git 설치 중..."
    sudo apt-get update -qq && sudo apt-get install -y -qq git
  fi
  if [[ -d "$CLONE_DIR/.git" ]]; then
    log "저장소가 이미 있습니다 → git pull ($CLONE_DIR)"
    git -C "$CLONE_DIR" pull --ff-only || warn "pull 실패 — 기존 코드로 계속합니다."
  else
    log "저장소 clone → $CLONE_DIR"
    git clone --depth 1 "$REPO_URL" "$CLONE_DIR"
  fi
  SCRIPT_DIR="$CLONE_DIR/deploy/alibaba"
  [[ -d "$SCRIPT_DIR" ]] || die "deploy/alibaba 를 찾을 수 없습니다: $SCRIPT_DIR"
fi

cd "$SCRIPT_DIR"
[[ -f "$COMPOSE_FILE" ]] || die "$COMPOSE_FILE 이 없습니다. 저장소 전체를 올렸는지 확인하세요."

# ── 2. Docker + compose 플러그인 (멱등) ───────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker 확인됨 — $(docker --version)"
else
  log "Docker 설치 중 (공식 apt 저장소)..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
  fi
  # dpkg 아키텍처를 그대로 쓴다 — amd64/arm64 양쪽에서 동작 (x86 고정 금지)
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
       docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
  log "Docker 설치 완료 — $(docker --version)"
fi

# 현재 사용자를 docker 그룹에 넣는다 (다음 로그인부터 sudo 없이 사용 가능)
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  sudo usermod -aG docker "$USER" && \
    warn "$USER 를 docker 그룹에 추가했습니다. 이번 실행은 sudo 로 진행하고, 다음부터는 재로그인 후 sudo 없이 쓰세요."
fi

# 이번 세션에서 docker 소켓에 바로 접근 가능한지 보고 필요하면 sudo 를 붙인다
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

# ── 3. .env 준비 (있으면 절대 덮어쓰지 않는다) ────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  log ".env 가 이미 있습니다 — 그대로 사용합니다."
else
  [[ -f "$ENV_EXAMPLE" ]] || die "$ENV_EXAMPLE 이 없습니다."
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  cat <<'EOF'

┌───────────────────────────────────────────────────────────────┐
│  .env 를 새로 만들었습니다. 시크릿을 채운 뒤 다시 실행하세요. │
└───────────────────────────────────────────────────────────────┘
    nano .env          # change-me-* 를 전부 실제 값으로 교체
    ./bootstrap.sh     # 다시 실행

  강한 값 생성:  openssl rand -hex 24
EOF
  exit 2
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

# change-me 가 남아 있으면 배포를 막는다 (약한 시크릿으로 인터넷에 노출 방지)
if grep -q 'change-me' "$ENV_FILE"; then
  grep -n 'change-me' "$ENV_FILE" | sed 's/=.*/=<미설정>/' >&2
  die ".env 에 change-me 자리표시자가 남아 있습니다. 위 항목을 실제 값으로 교체하세요."
fi

# ── 4. 기동 ───────────────────────────────────────────────────────────────
log "compose 파일 검증..."
"${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" config -q \
  || die "compose 설정 오류 — 위 메시지를 확인하세요 (대개 .env 누락 변수)."

log "이미지 빌드 및 기동 (최초 실행은 수 분 걸립니다)..."
"${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build --remove-orphans

# ── 5. 헬스 대기 ──────────────────────────────────────────────────────────
log "서비스 health 대기 (최대 ${HEALTH_TIMEOUT}s)..."
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
  api_ok=$(curl -fsS -m 3 http://127.0.0.1:8100/health  >/dev/null 2>&1 && echo yes || echo no)
  evo_ok=$(curl -fsS -m 3 http://127.0.0.1:8200/api/health >/dev/null 2>&1 && echo yes || echo no)
  [[ "$api_ok" == yes && "$evo_ok" == yes ]] && break
  if (( $(date +%s) >= deadline )); then
    warn "제한 시간 내 health 응답 없음 (api=$api_ok evolution=$evo_ok)."
    "${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
    echo "로그 확인:  docker compose -f $COMPOSE_FILE logs --tail=80 api evolution"
    exit 1
  fi
  sleep 5
done

# ── 6. 접속 주소 안내 ─────────────────────────────────────────────────────
# Alibaba Cloud 메타데이터 서비스 → 실패 시 외부 에코 서비스로 폴백
PUBLIC_IP="$(curl -fsS -m 3 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null \
  || curl -fsS -m 3 http://100.100.100.200/latest/meta-data/public-ipv4 2>/dev/null \
  || curl -fsS -m 5 https://api.ipify.org 2>/dev/null || echo '<ECS-공인IP>')"

cat <<EOF

========================================================================
 배포 완료 — 두 서비스가 정상 응답합니다.
========================================================================
  운영 서버(ops)      http://${PUBLIC_IP}:8100        health: /health
  자가진화 서버        http://${PUBLIC_IP}:8200        health: /api/health

  * 보안그룹 인바운드에 8100 / 8200 이 열려 있어야 외부에서 보입니다.
  * postgres / redis 는 호스트 포트를 열지 않았습니다 (설계상 정상).

  검증:  ./verify_deployment.sh http://${PUBLIC_IP}
  상태:  docker compose -f ${COMPOSE_FILE} ps
  로그:  docker compose -f ${COMPOSE_FILE} logs -f api evolution
========================================================================
EOF
