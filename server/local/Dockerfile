# python:3.12-slim — 멀티아키(amd64/arm64) 공식 이미지.
# Alibaba Cloud ECS 이전이 전제 — 멀티아키(x86_64/ARM64) 유지, x86 전용 바이너리 설치 금지 (SPEC-00 §3.2).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
# 운영 도구 — 데모 시딩을 `docker compose exec api python tools/seed_demo_sessions.py`
# 로 돌린다. tests/(560K·pytest 의존)는 넣지 않는다: 시딩 도구는 tests/ 대신 app.* 만
# 참조하도록 자체 완결로 작성했다.
COPY tools ./tools

EXPOSE 8100
# 컨테이너 내부는 0.0.0.0 바인딩 — 호스트 노출은 compose 의 "127.0.0.1:8100:8100" 매핑이 제한한다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
