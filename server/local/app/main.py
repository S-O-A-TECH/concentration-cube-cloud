"""FastAPI 앱 조립 — 라우터는 S 단계별로 추가된다 (S3 results, S5 evolution, S6 ops)."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.app_api import router as app_router
from app.api.billing import router as billing_router
from app.api.devices import router as devices_router
from app.api.evolution import router as evolution_router
from app.api.health import router as health_router
from app.api.ops import protected as ops_protected_router
from app.api.ops import router as ops_router
from app.api.relax_voice import app_router as relax_voice_app_router
from app.api.relax_voice import ops_router as relax_voice_ops_router
from app.api.sessions import router as sessions_router
from app.ops_ui.router import router as ops_ui_router

MAX_BODY_BYTES = 10 * 1024 * 1024   # 10MB (S2 계획 §1.6)
# 수면세션 음성(mp3) 업로드만 multipart 예외 — 상한을 15MB(+multipart 오버헤드)로 올린다.
# video/* 는 이 경로에서도 절대 허용하지 않는다 (raw video-free 원칙 불변).
RELAX_VOICE_UPLOAD_PREFIX = "/v1/ops/relax_voice/"
RELAX_VOICE_MAX_BYTES = 16 * 1024 * 1024


def _check_secrets() -> None:
    """기본 시크릿 감시 (S7 §H1) — 로컬은 경고, REQUIRE_SECURE_SECRETS=true 면 부팅 거부."""
    from app.config import get_settings
    s = get_settings()
    insecure = [name for name, v in [("JWT_SECRET", s.jwt_secret),
                                     ("EVOLUTION_TOKEN", s.evolution_token),
                                     ("OPS_ADMIN_PW", s.ops_admin_pw),
                                     ("BILLING_WEBHOOK_SECRET", s.billing_webhook_secret)]
                if v in ("change-me", "")]
    if not insecure:
        return
    msg = f"기본/빈 시크릿 사용 중: {', '.join(insecure)} — .env 에서 교체하세요"
    if s.require_secure_secrets:
        raise RuntimeError(f"[SECURITY] {msg} (REQUIRE_SECURE_SECRETS=true)")
    print(f"[SECURITY WARNING] {msg} (로컬 개발 한정 허용)")


_check_secrets()

app = FastAPI(title="concentration-cube server", version="0.1.0")


@app.middleware("http")
async def reject_video_and_oversize(request: Request, call_next):
    """영상 무수신 원칙 — video MIME 거부 + multipart 화이트리스트 + 요청 크기 상한 (SPEC-02 §3).

    multipart 는 원칙적으로 거부하되(이 서버엔 바이너리 업로드가 거의 없다), 수면세션 음성
    업로드 경로(RELAX_VOICE_UPLOAD_PREFIX)만 mp3 업로드를 위해 예외로 허용한다. video/* 는
    그 경로를 포함해 어디서도 절대 허용하지 않는다 — 이 두 규칙은 항상 한 쌍으로 유지한다.
    """
    content_type = request.headers.get("content-type", "")
    is_voice_upload = request.url.path.startswith(RELAX_VOICE_UPLOAD_PREFIX)
    # ① video/* 는 경로 불문 영원히 거부 (raw video-free 구조적 가드)
    if content_type.startswith("video/"):
        return JSONResponse(status_code=415,
                            content={"detail": "video uploads are never accepted"})
    # ② multipart 는 오직 수면세션 음성(mp3) 업로드 경로에서만 허용, 그 외 전부 415
    if content_type.startswith("multipart/") and not is_voice_upload:
        return JSONResponse(status_code=415,
                            content={"detail": "multipart uploads are not accepted here"})
    # ③ 요청 크기 상한 — 음성 업로드만 15MB(+multipart 오버헤드), 그 외 10MB
    limit = RELAX_VOICE_MAX_BYTES if is_voice_upload else MAX_BODY_BYTES
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        return JSONResponse(status_code=413,
                            content={"detail": f"body exceeds {limit} bytes"})
    return await call_next(request)


app.include_router(health_router)
app.include_router(devices_router)
app.include_router(sessions_router)
app.include_router(evolution_router)
app.include_router(ops_router)
app.include_router(ops_protected_router)
app.include_router(relax_voice_ops_router)   # /v1/ops/relax_voice (수면세션 음성 배포)
app.include_router(ops_ui_router)
app.include_router(app_router)       # 앱 pull (계정 JWT — 2026-07-07)
app.include_router(relax_voice_app_router)   # /v1/app/enhance/relax_voice (앱 pull)
app.include_router(billing_router)   # 결제 웹훅 스텁 (PG 확정 전 일반형)
