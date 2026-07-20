"""수면세션 음성 배포 — /v1/ops/relax_voice(운영자) + /v1/app/enhance/relax_voice(앱).

검증(2026-07-07 요구): draft→업로드→publish(과거일) 앱 active·mp3 200 / 미래일은
비활성·파일 404 / draft 파일 앱 404 / video·비mp3 거부(가드 정밀화가 video 거부를 완화하지
않음) / /ops/relax_voice 렌더 / 앱 계약 응답 형태.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_nonce_store, get_storage
from app.config import get_settings
from app.db import models
from app.db.base import Base
from app.db.seed import seed
from app.main import app
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage

# 최소 유효 mp3 바이트 — ID3 시그니처(looks_like_mp3 통과)
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 2048


@pytest.fixture()
def env(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = SessionStorage(tmp_path / "storage")

    with factory() as db:
        seed(db)
        db.commit()

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_nonce_store] = lambda: MemoryNonceStore()

    client = TestClient(app)
    yield {"client": client, "factory": factory, "storage": storage}
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client) -> None:
    s = get_settings()
    r = client.post("/v1/ops/login", json={"id": s.ops_admin_id, "pw": s.ops_admin_pw})
    assert r.status_code == 200


def _app_hdr(client) -> dict:
    r = client.post("/v1/ops/users", json={"email": "parent@example.com"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['app_token']}"}


def _new_draft(client, note="1차", protocol="short") -> int:
    r = client.post("/v1/ops/relax_voice/releases",
                    json={"note": note, "protocol": protocol})
    assert r.status_code == 200, r.text
    assert r.json()["protocol"] == protocol
    return r.json()["id"]


def _upload(client, rid, slot, data=MP3, ctype="audio/mpeg", name=None):
    return client.post(f"/v1/ops/relax_voice/releases/{rid}/upload/{slot}",
                       files={"file": (name or f"{slot}.mp3", data, ctype)})


def test_full_publish_flow_app_pull(env):
    """draft→2슬롯 업로드→과거일 publish→앱 active(slots 2개)·mp3 200·published 불변."""
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    # 초기엔 배포 중 없음
    assert client.get("/v1/app/enhance/relax_voice", headers=hdr).json() == {"active": None}

    rid = _new_draft(client)
    for slot in ("close_eyes", "pmr"):
        assert _upload(client, rid, slot).status_code == 200

    # draft 단계 — 앱은 아직 못 본다 + draft 파일 앱 404
    assert client.get("/v1/app/enhance/relax_voice", headers=hdr).json()["active"] is None
    assert client.get(f"/v1/app/enhance/relax_voice/{rid}/close_eyes.mp3",
                      headers=hdr).status_code == 404

    # 과거 날짜로 배포
    r = client.post(f"/v1/ops/relax_voice/releases/{rid}/publish",
                    json={"release_date": "2020-01-01"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published" and r.json()["is_active"] is True

    # 앱 active — 업로드된 2슬롯만, 계약 URL 형태 그대로
    active = client.get("/v1/app/enhance/relax_voice", headers=hdr).json()["active"]
    assert active["release_id"] == rid
    assert set(active["slots"].keys()) == {"close_eyes", "pmr"}
    assert active["slots"]["close_eyes"] == f"/v1/app/enhance/relax_voice/{rid}/close_eyes.mp3"
    assert "breathing" not in active["slots"] and "closing" not in active["slots"]
    assert active["release_at"].startswith("2020-01-01")  # KST 배포일

    # mp3 GET 200 + audio/mpeg + 바이트 일치
    fr = client.get(f"/v1/app/enhance/relax_voice/{rid}/close_eyes.mp3", headers=hdr)
    assert fr.status_code == 200
    assert fr.headers["content-type"] == "audio/mpeg"
    assert fr.content == MP3

    # 미업로드 슬롯 파일은 404
    assert client.get(f"/v1/app/enhance/relax_voice/{rid}/breathing.mp3",
                      headers=hdr).status_code == 404

    # published 릴리즈는 불변 — 업로드/재배포 거부
    assert _upload(client, rid, "breathing").status_code == 409
    assert client.post(f"/v1/ops/relax_voice/releases/{rid}/publish",
                       json={"release_date": "2021-01-01"}).status_code == 409


def test_future_release_not_active(env):
    """미래 배포일 릴리즈는 앱 active 로 안 잡히고 파일도 404 (단 ops 미리듣기는 200)."""
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    rid = _new_draft(client, "미래")
    assert _upload(client, rid, "closing").status_code == 200
    assert client.post(f"/v1/ops/relax_voice/releases/{rid}/publish",
                       json={"release_date": "2999-12-31"}).status_code == 200

    # 미래 배포일 → active 아님, 앱 파일 404
    assert client.get("/v1/app/enhance/relax_voice", headers=hdr).json()["active"] is None
    assert client.get(f"/v1/app/enhance/relax_voice/{rid}/closing.mp3",
                      headers=hdr).status_code == 404

    # 운영자 미리듣기는 배포일 무관 200
    assert client.get(f"/v1/ops/relax_voice/{rid}/closing.mp3").status_code == 200


def test_latest_released_wins(env):
    """past 릴리즈가 여럿이면 release_at 최신이 active."""
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    older = _new_draft(client, "구버전")
    _upload(client, older, "pmr")
    client.post(f"/v1/ops/relax_voice/releases/{older}/publish",
                json={"release_date": "2020-01-01"})

    newer = _new_draft(client, "신버전")
    _upload(client, newer, "pmr")
    _upload(client, newer, "breathing")
    client.post(f"/v1/ops/relax_voice/releases/{newer}/publish",
                json={"release_date": "2021-06-01"})

    active = client.get("/v1/app/enhance/relax_voice", headers=hdr).json()["active"]
    assert active["release_id"] == newer
    assert set(active["slots"].keys()) == {"pmr", "breathing"}


def test_video_and_non_mp3_rejected(env):
    """video·비mp3 거부 — 가드 정밀화가 video 거부를 완화하지 않는다."""
    client = env["client"]
    _login(client)
    rid = _new_draft(client, "가드")

    # ① video/* content-type → 미들웨어 415 (음성 경로 포함 경로 불문 거부)
    assert client.post(f"/v1/ops/relax_voice/releases/{rid}/upload/close_eyes",
                       content=b"x",
                       headers={"content-type": "video/mp4"}).status_code == 415

    # ② multipart 인데 파트가 video/* → 엔드포인트 400 (audio/* 아님)
    assert _upload(client, rid, "close_eyes", data=b"\x00\x00\x00\x18ftypmp42",
                   ctype="video/mp4", name="x.mp4").status_code == 400

    # ③ audio content-type 이지만 mp3 시그니처 아님 → 400
    assert _upload(client, rid, "close_eyes", data=b"not-an-mp3-at-all",
                   ctype="audio/mpeg").status_code == 400

    # ④ 빈 슬롯 이름 → 422
    assert _upload(client, rid, "nope").status_code == 422

    # ⑤ 세션 경로의 multipart 는 여전히 415 (가드가 다른 경로엔 그대로)
    assert client.post("/v1/sessions/start", content=b"x",
                       headers={"content-type": "multipart/form-data; boundary=z"}
                       ).status_code == 415


def test_auth_required(env):
    """ops 는 쿠키, 앱은 토큰 없이는 각각 401."""
    client = env["client"]
    # ops 미로그인
    assert client.get("/v1/ops/relax_voice").status_code == 401
    assert client.post("/v1/ops/relax_voice/releases", json={}).status_code == 401
    # 앱 무토큰
    assert client.get("/v1/app/enhance/relax_voice").status_code == 401
    assert client.get("/v1/app/enhance/relax_voice/1/close_eyes.mp3").status_code == 401


def test_ops_page_renders_and_publish_guard(env):
    """/ops/relax_voice Jinja 렌더 200 + 빈 릴리즈 배포는 422."""
    client = env["client"]
    _login(client)

    # 빈 페이지 렌더
    assert client.get("/ops/relax_voice").status_code == 200

    rid = _new_draft(client, "렌더")
    # 업로드 없이 배포 → 422
    assert client.post(f"/v1/ops/relax_voice/releases/{rid}/publish",
                       json={"release_date": "2020-01-01"}).status_code == 422

    _upload(client, rid, "pmr")
    client.post(f"/v1/ops/relax_voice/releases/{rid}/publish",
                json={"release_date": "2020-01-01"})
    # 배포 후에도 렌더 200 (active/이력 표시)
    assert client.get("/ops/relax_voice").status_code == 200

    # publish/upload 감사로그 기록 확인
    audit = client.get("/v1/ops/audit").json()["audit"]
    actions = {a["action"] for a in audit}
    assert "relax_voice_published" in actions
    assert "relax_voice_uploaded" in actions


# ============================================================ 긴 버전(5분)

LONG_SLOTS = ("close_eyes", "stretch_1", "breathing_1", "meditation_1",
              "stretch_2", "breathing_2", "meditation_2")


def test_long_release_independent_from_short(env):
    """긴 버전 draft→7슬롯 중 2개 업로드→과거일 publish→?protocol=long 에만 active.

    short 는 영향받지 않고, long 슬롯 계약(7키)이 그대로 노출된다.
    """
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    rid = _new_draft(client, "긴버전1", protocol="long")
    for slot in ("close_eyes", "breathing_1"):
        assert _upload(client, rid, slot).status_code == 200
    # short 슬롯을 long 릴리즈에 올리면 422 (protocol 계약 밖)
    assert _upload(client, rid, "pmr").status_code == 422

    assert client.post(f"/v1/ops/relax_voice/releases/{rid}/publish",
                       json={"release_date": "2020-01-01"}).status_code == 200

    # ?protocol=long → active, slots 2개 (long URL)
    active = client.get("/v1/app/enhance/relax_voice?protocol=long",
                        headers=hdr).json()["active"]
    assert active["release_id"] == rid
    assert set(active["slots"].keys()) == {"close_eyes", "breathing_1"}
    assert active["slots"]["breathing_1"] == \
        f"/v1/app/enhance/relax_voice/{rid}/breathing_1.mp3"

    # short 는 여전히 배포 없음 (독립)
    assert client.get("/v1/app/enhance/relax_voice", headers=hdr).json()["active"] is None
    assert client.get("/v1/app/enhance/relax_voice?protocol=short",
                      headers=hdr).json()["active"] is None

    # long mp3 GET 200
    fr = client.get(f"/v1/app/enhance/relax_voice/{rid}/breathing_1.mp3", headers=hdr)
    assert fr.status_code == 200 and fr.headers["content-type"] == "audio/mpeg"


def test_long_protocol_seed_matches_default(env):
    """앱이 pull 하는 긴 버전 프로토콜이 요구된 기본 JSON 과 정확히 일치."""
    from app.services.relax_voice import DEFAULT_RELAX_LONG_PROTOCOL
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    proto = client.get("/v1/app/enhance/relax_long_protocol", headers=hdr).json()
    assert proto == DEFAULT_RELAX_LONG_PROTOCOL
    # 스키마 확언 — 8단계, 5분(300초), long 슬롯 7키가 voiceSlot 로 등장
    assert proto["id"] == "relax_long_v1"
    assert len(proto["steps"]) == 8
    assert sum(s["seconds"] for s in proto["steps"]) == 300
    voice_slots = [s["params"]["voiceSlot"] for s in proto["steps"]
                   if "voiceSlot" in s.get("params", {})]
    assert voice_slots == list(LONG_SLOTS)


def test_long_protocol_sequence_edit(env):
    """시퀀스 편집 저장 → relax_long_protocol 에 초·음악 반영, 호흡은 reps 로 환산."""
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    editor = client.get("/v1/ops/relax_voice/long_protocol").json()
    rows = editor["rows"]
    assert len(rows) == 8
    assert "이어서(변경 없음)" in editor["music_options"]

    # 각 단계의 현재 값을 그대로 두되, 몇 개만 바꾼다:
    #  - step0(close_eyes 음악): 느린 파도 → 여린 시냇물
    #  - step2(breathing_1): 30초 → 55초 (reps=round(55/10)=6 → 60초로 스냅)
    #  - step1(stretch_1): 음악 없음 → "느린 파도"
    steps = []
    for row in rows:
        seconds = row["seconds"]
        music = row["music"]
        if row["index"] == 0:
            music = "여린 시냇물"
        elif row["index"] == 1:
            music = "느린 파도"
        elif row["index"] == 2:
            seconds = 55
        steps.append({"seconds": seconds, "music": music})

    r = client.post("/v1/ops/relax_voice/long_protocol", json={"steps": steps})
    assert r.status_code == 200, r.text

    proto = client.get("/v1/app/enhance/relax_long_protocol", headers=hdr).json()
    s0, s1, s2 = proto["steps"][0], proto["steps"][1], proto["steps"][2]
    assert s0["params"]["sound"] == "assets/sounds/relax_stream.mp3"   # 여린 시냇물
    assert s1["params"]["sound"] == "assets/sounds/relax_waves.mp3"    # 느린 파도(신규)
    # 호흡: 55초 → reps=6, seconds=60 으로 환산, sound 는 보존
    assert s2["params"]["reps"] == 6
    assert s2["seconds"] == 60
    assert s2["params"]["sound"] == "assets/sounds/relax_breath.mp3"
    # 단계 종류·순서·텍스트·voiceSlot 은 보존
    assert [s["kind"] for s in proto["steps"]] == \
        ["message", "stretch", "breathing", "message", "stretch",
         "breathing", "message", "fadeout"]
    assert s2["params"]["voiceSlot"] == "breathing_1"

    # 편집 감사로그
    actions = {a["action"] for a in client.get("/v1/ops/audit").json()["audit"]}
    assert "relax_long_protocol_edited" in actions


def test_long_protocol_continue_music_removes_sound(env):
    """'이어서(변경 없음)' 선택 시 params.sound 제거(직전 음악 유지)."""
    client = env["client"]
    _login(client)
    hdr = _app_hdr(client)

    rows = client.get("/v1/ops/relax_voice/long_protocol").json()["rows"]
    steps = []
    for row in rows:
        music = "이어서(변경 없음)" if row["index"] == 0 else row["music"]
        steps.append({"seconds": row["seconds"], "music": music})
    assert client.post("/v1/ops/relax_voice/long_protocol",
                       json={"steps": steps}).status_code == 200

    proto = client.get("/v1/app/enhance/relax_long_protocol", headers=hdr).json()
    assert "sound" not in proto["steps"][0]["params"]     # 제거됨


def test_ops_page_renders_two_sections(env):
    """/ops/relax_voice 가 짧은/긴 두 섹션 + 시퀀스 편집기를 렌더."""
    client = env["client"]
    _login(client)
    # 각 버전에 draft 하나씩
    _new_draft(client, "s", protocol="short")
    _new_draft(client, "l", protocol="long")
    r = client.get("/ops/relax_voice")
    assert r.status_code == 200
    body = r.text
    assert "짧은 버전" in body and "긴 버전" in body
    assert "시퀀스 편집" in body           # 긴 버전 편집기 렌더
    assert "가벼운 스트레칭 ①" in body      # long 슬롯 라벨


def test_long_protocol_endpoints_auth(env):
    """긴 버전 엔드포인트도 ops 쿠키·앱 토큰 인증을 요구한다."""
    client = env["client"]
    assert client.get("/v1/ops/relax_voice/long_protocol").status_code == 401
    assert client.post("/v1/ops/relax_voice/long_protocol",
                       json={"steps": []}).status_code == 401
    assert client.get("/v1/app/enhance/relax_long_protocol").status_code == 401
