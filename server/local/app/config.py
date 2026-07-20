"""환경설정 (.env) — SPEC-00 §4.

기본값은 '호스트 직접 실행 모드'(README 실행법 ②) 기준이다:
DB/redis 는 컨테이너(127.0.0.1:5433 / 6379), api 는 호스트 uvicorn.
docker compose 실행 시에는 compose 가 컨테이너 내부 주소로 전부 덮어쓴다.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://cube:cube_dev_pw@127.0.0.1:5433/cube"
    redis_url: str = "redis://127.0.0.1:6380/0"  # 6379 는 WSL 내부 redis 가 점유 — 컨테이너 노출은 6380
    storage_root: str = "./data/storage"
    app_version: str = "dev"

    # 시크릿 자리 — 실값은 .env 로만 (커밋 금지)
    ops_admin_id: str = "admin"          # S6 운영 관리자 페이지 로그인
    ops_admin_pw: str = "change-me"
    evolution_token: str = "change-me"   # S5 /v1/evolution/* 인증 (X-Evolution-Token)
    jwt_secret: str = "change-me"        # S2 기기 JWT 서명
    billing_webhook_secret: str = "change-me"  # /v1/billing/webhook (PG 스텁 — 2026-07-07)

    # true 면 기본 시크릿("change-me")으로 부팅 거부 — 클라우드(S8)에서 필수로 켠다
    require_secure_secrets: bool = False

    # LLM 리포트 (S3 — worker 전용, 수치 요약만 전송: SPEC-00 §2)
    # Qwen (Alibaba Cloud Model Studio, OpenAI 호환) — 워크스페이스 전용 URL 은 .env 로 덮어쓴다
    llm_report_enabled: bool = False
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"   # API 는 소문자 id 만 허용


@lru_cache
def get_settings() -> Settings:
    return Settings()
