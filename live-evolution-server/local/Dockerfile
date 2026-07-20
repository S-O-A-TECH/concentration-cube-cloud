# python:3.12-slim — 멀티아키(amd64/arm64) 공식 이미지.
# server/local/Dockerfile 과 동일 스타일 — x86 전용 바이너리 설치 금지 (SPEC-00 §3.2).
# pandas/numpy/pyarrow 는 amd64·aarch64 모두 manylinux 휠이 있어 컴파일러가 필요 없다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt

# app/webui 는 StaticFiles 로 마운트되므로 app 패키지 전체를 넣는다 (main.py §static).
COPY app ./app
COPY tools ./tools

# 런타임 상태(state.sqlite·secret.key·workspaces)는 LEV_STATE_DIR 로 볼륨에 둔다.
# 미지정 시 config.py 가 /srv/state 를 쓰는데, 그러면 컨테이너 재생성 시 사라진다.
ENV LEV_STATE_DIR=/data/state

EXPOSE 8200
# run.py 는 127.0.0.1 고정 바인딩(SPEC-04 §2, 로컬 전용)이라 컨테이너에서 쓸 수 없다.
# 외부 노출 범위는 compose 의 포트 매핑과 ECS 보안그룹이 제한한다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
