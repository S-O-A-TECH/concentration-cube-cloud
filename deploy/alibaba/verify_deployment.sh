#!/usr/bin/env bash
# concentration-cube — Alibaba Cloud ECS 배포 검증 (해커톤 배포 증빙 겸용).
#
# 검사 4가지:
#   1) 운영 서버 /health          — db·redis·storage 하위 항목까지 확인
#   2) 자가진화 서버 /api/health
#   3) 자가진화 → 운영 서버 내부 연동 (컨테이너 네트워크로 실제 호출되는지)
#   4) Qwen 실호출 — 배포된 API 를 통해 Alibaba Cloud Model Studio 에 실제 요청
#      (POST /api/agents/qwen/check → 어댑터가 chat/completions 를 1회 호출)
#
# 사용법:
#   ./verify_deployment.sh                      # 로컬(ECS 안)에서 127.0.0.1 대상
#   ./verify_deployment.sh http://<ECS-공인IP>  # 외부에서 공인 IP 대상
#
# 종료코드 0 = 전체 PASS. 증빙 저장:  ./verify_deployment.sh | tee proof.txt
set -uo pipefail   # -e 는 쓰지 않는다 — 실패한 검사도 끝까지 보고해야 한다

BASE="${1:-http://127.0.0.1}"
BASE="${BASE%/}"
OPS_URL="${BASE}:8100"
EVO_URL="${BASE}:8200"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

PASS=0; FAIL=0
green() { printf '\033[1;32m%s\033[0m' "$*"; }
red()   { printf '\033[1;31m%s\033[0m' "$*"; }

ok()   { PASS=$((PASS+1)); printf '  [%s] %s\n' "$(green PASS)" "$1"; }
no()   { FAIL=$((FAIL+1)); printf '  [%s] %s\n' "$(red FAIL)" "$1"; [[ -n "${2:-}" ]] && printf '         └ %s\n' "$2"; }

# JSON 에서 키 하나 뽑기 (jq 의존 없이 — 기본 Ubuntu 이미지에 jq 가 없다)
# 불리언은 반드시 JSON 표기(true/false)로 낮춰 출력한다 — 파이썬 기본 str() 은 "True" 라
# 아래 비교문들이 전부 어긋난다.
jget() { python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
for k in sys.argv[1].split("."):
    if isinstance(d,dict) and k in d: d=d[k]
    else: sys.exit(1)
if isinstance(d,bool): print("true" if d else "false")
elif isinstance(d,(dict,list)): print(json.dumps(d,ensure_ascii=False))
else: print(d)' "$1" 2>/dev/null; }

echo "========================================================================"
echo " concentration-cube — Alibaba Cloud ECS 배포 검증"
echo " 대상: ops=${OPS_URL}  evolution=${EVO_URL}"
echo " 시각: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================================================"

# ── 1. 운영 서버 /health ─────────────────────────────────────────────────
echo
echo "[1/4] 운영 서버 health"
OPS_BODY="$(curl -fsS -m 10 "${OPS_URL}/health" 2>&1)"
if [[ $? -eq 0 && -n "$OPS_BODY" ]]; then
  ok "GET ${OPS_URL}/health → 200"
  echo "         응답: ${OPS_BODY:0:300}"
  for part in db redis storage; do
    v="$(printf '%s' "$OPS_BODY" | jget "$part")"
    case "$v" in
      ok|true|True|OK) ok "  하위 점검 ${part} = ${v}" ;;
      "")              : ;;   # 해당 키가 없는 응답 형식이면 조용히 넘어간다
      *)               no "  하위 점검 ${part} = ${v}" ;;
    esac
  done
else
  no "GET ${OPS_URL}/health 실패" "${OPS_BODY:0:200}"
fi

# ── 2. 자가진화 서버 /api/health ─────────────────────────────────────────
echo
echo "[2/4] 자가진화 서버 health"
EVO_BODY="$(curl -fsS -m 10 "${EVO_URL}/api/health" 2>&1)"
if [[ $? -eq 0 ]] && [[ "$(printf '%s' "$EVO_BODY" | jget app)" == "live-evolution" ]]; then
  ok "GET ${EVO_URL}/api/health → 200 (app=live-evolution)"
  echo "         응답: ${EVO_BODY:0:300}"
else
  no "GET ${EVO_URL}/api/health 실패" "${EVO_BODY:0:200}"
fi

# ── 로그인 (3·4번 검사에 필요) ───────────────────────────────────────────
# .env 의 진화 서버 계정을 읽는다. 없으면 환경변수로 넘길 수 있다.
if [[ -f "$ENV_FILE" ]]; then
  EVO_ID="${EVO_ADMIN_ID:-$(grep -E '^EVO_ADMIN_ID=' "$ENV_FILE" | cut -d= -f2-)}"
  EVO_PW="${EVO_ADMIN_PW:-$(grep -E '^EVO_ADMIN_PW=' "$ENV_FILE" | cut -d= -f2-)}"
else
  EVO_ID="${EVO_ADMIN_ID:-}"; EVO_PW="${EVO_ADMIN_PW:-}"
fi

LOGGED_IN=no
if [[ -n "$EVO_ID" && -n "$EVO_PW" ]]; then
  LOGIN_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -c "$COOKIE_JAR" \
    -X POST "${EVO_URL}/api/login" -H 'Content-Type: application/json' \
    --data "$(python3 -c 'import json,sys; print(json.dumps({"id":sys.argv[1],"pw":sys.argv[2]}))' "$EVO_ID" "$EVO_PW")" 2>/dev/null)"
  [[ "$LOGIN_CODE" == "200" ]] && LOGGED_IN=yes
fi

# ── 3. 자가진화 → 운영 서버 내부 연동 ────────────────────────────────────
echo
echo "[3/4] 자가진화 → 운영 서버 내부 연동 (컨테이너 네트워크)"
if [[ "$LOGGED_IN" == yes ]]; then
  DASH="$(curl -fsS -m 20 -b "$COOKIE_JAR" "${EVO_URL}/api/dashboard" 2>&1)"
  OPS_OK="$(printf '%s' "$DASH" | jget ops.ok)"
  OPS_SEEN="$(printf '%s' "$DASH" | jget ops.url)"
  if [[ "$OPS_OK" == "true" ]]; then
    ok "진화 서버가 운영 서버에 도달 (SERVER_URL=${OPS_SEEN})"
  else
    no "진화 서버 → 운영 서버 연동 실패 (SERVER_URL=${OPS_SEEN:-?})" \
       "$(printf '%s' "$DASH" | jget ops.error)"
  fi
else
  no "진화 서버 로그인 실패 — 연동 검사 건너뜀" \
     "EVO_ADMIN_ID/EVO_ADMIN_PW 가 .env 와 일치하는지 확인하세요 (HTTP ${LOGIN_CODE:-없음})."
fi

# ── 4. Qwen 실호출 (Alibaba Cloud Model Studio) ──────────────────────────
echo
echo "[4/4] Qwen 실호출 — 배포된 API 를 통해 Model Studio 호출"
if [[ "$LOGGED_IN" == yes ]]; then
  # 어댑터의 check_auth() 가 chat/completions 로 초소형 실요청을 1회 보낸다.
  QWEN="$(curl -fsS -m 60 -b "$COOKIE_JAR" -X POST "${EVO_URL}/api/agents/qwen/check" 2>&1)"
  AUTH_OK="$(printf '%s' "$QWEN" | jget auth_ok)"
  DETAIL="$(printf '%s' "$QWEN" | jget auth_detail)"
  CMD="$(printf '%s' "$QWEN" | jget auth_command)"
  if [[ "$AUTH_OK" == "true" ]]; then
    ok "Qwen API 실호출 성공 — ${DETAIL}"
    echo "         엔드포인트: ${CMD}"
    echo "         확인시각:   $(printf '%s' "$QWEN" | jget checked_at)"
  else
    no "Qwen API 실호출 실패" "${DETAIL:-${QWEN:0:200}}"
  fi
else
  no "진화 서버 로그인 실패 — Qwen 검사 건너뜀"
fi

# ── 요약 ─────────────────────────────────────────────────────────────────
echo
echo "========================================================================"
if (( FAIL == 0 )); then
  printf ' 결과: %s  (통과 %d건)\n' "$(green '전체 PASS')" "$PASS"
  echo " → Alibaba Cloud ECS 배포가 정상 동작하며 Qwen 실호출까지 확인되었습니다."
  echo "========================================================================"
  exit 0
else
  printf ' 결과: %s  (통과 %d건 / 실패 %d건)\n' "$(red 'FAIL')" "$PASS" "$FAIL"
  echo " → 트러블슈팅: README.md 의 '문제 해결' 절을 참고하세요."
  echo "   로그:  docker compose -f docker-compose.cloud.yml logs --tail=80 api evolution"
  echo "========================================================================"
  exit 1
fi
