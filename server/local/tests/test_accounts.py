"""가족 계정·구독·앱 pull — 2026-07-07 결정 사항 검증.

결정: 보호자 1 : 학생 프로필 ≤3 : 기기 무제한 / 구독 수동+웹훅 스텁 /
앱은 pull(콘솔과 동일 정본 JSON) / 전체 열람+감사로그.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_nonce_store, get_storage
from app.config import get_settings
from app.db import models
from app.db.base import Base
from app.db.seed import seed
from app.jobs.score_session import score_session
from app.main import app
from app.services import queue as queue_service
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage
from tests.device_sim import DeviceSim

SERIAL = "SIM_DEV_ACC"
FACTORY_TOKEN = "sim-acc-token"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = SessionStorage(tmp_path / "storage")

    with factory() as db:
        seed(db)
        db.add(models.Device(
            serial=SERIAL,
            factory_token_hash=hashlib.sha256(FACTORY_TOKEN.encode()).hexdigest()))
        db.commit()

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_nonce_store] = lambda: MemoryNonceStore()
    monkeypatch.setattr(queue_service, "enqueue_score_session",
                        lambda sid: score_session(sid, _factory=factory, _storage=storage))
    monkeypatch.setattr(queue_service, "enqueue_llm_report", lambda run_id: None)

    client = TestClient(app)
    yield {"client": client, "factory": factory}
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client) -> None:
    s = get_settings()
    r = client.post("/v1/ops/login", json={"id": s.ops_admin_id, "pw": s.ops_admin_pw})
    assert r.status_code == 200


def _mk_account(client, email="parent@example.com") -> dict:
    r = client.post("/v1/ops/users", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()


def test_family_account_lifecycle(env):
    """계정 생성 → 프로필 3명(4번째 409) → 기기 바인딩 → 세션 귀속 → 상세 시간순."""
    client = env["client"]
    _login(client)

    acc = _mk_account(client)
    uid = acc["id"]
    assert acc["app_token"]  # 1회 노출

    # 프로필 3명까지 — 4번째는 409 (가족 계정 상한)
    pids = []
    for name in ("첫째", "둘째", "셋째"):
        r = client.post(f"/v1/ops/users/{uid}/profiles", json={"nickname": name})
        assert r.status_code == 200
        pids.append(r.json()["id"])
    assert client.post(f"/v1/ops/users/{uid}/profiles",
                       json={"nickname": "넷째"}).status_code == 409

    # 기기 바인딩 — 세션이 학생 프로필로 귀속되기 시작한다
    with env["factory"]() as db:
        dev_id = db.execute(select(models.Device)
                            .where(models.Device.serial == SERIAL)).scalar_one().id
    r = client.post(f"/v1/ops/devices/{dev_id}/bind",
                    json={"user_id": uid, "default_profile_id": pids[0]})
    assert r.status_code == 200

    # 타 계정 프로필로 바인딩 시도 → 422
    other = _mk_account(client, "other@example.com")
    r_other = client.post(f"/v1/ops/users/{other['id']}/profiles",
                          json={"nickname": "남의집아이"})
    assert client.post(f"/v1/ops/devices/{dev_id}/bind",
                       json={"user_id": uid,
                             "default_profile_id": r_other.json()["id"]}).status_code == 422

    # 기기 세션 업로드 → 자동 채점 → 계정 상세에 시간순으로 나타난다
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    sid = sim.run_session([("focus", 90), ("blank_stare", 30)])["sid"]

    detail = client.get(f"/v1/ops/users/{uid}").json()
    assert detail["profile_limit"] == 3
    assert [d["serial"] for d in detail["devices"]] == [SERIAL]
    first = next(p for p in detail["profiles"] if p["id"] == pids[0])
    assert [s["sid"] for s in first["sessions"]] == [sid]
    sess = first["sessions"][0]
    assert sess["engine"] == "v1.0"          # 채점 엔진 버전 뱃지 (소급 재채점 추적)
    assert sess["sfi"] is not None

    # 열람 감사로그 (전체 열람+감사 결정)
    audit = client.get("/v1/ops/audit").json()["audit"]
    assert any(a["action"] == "account_viewed" and a["target"] == f"user:{uid}"
               for a in audit)


def test_pupil_missing_alarm_on_production_device(env):
    """정식 기기(NIR)인데 동공 없이 채점되면 리포트/대시보드에 알람이 뜬다(2026-07-07).
    테스트 기기(웹캠/가상)는 동공 부재가 전제라 알람 대상이 아니다."""
    client = env["client"]
    _login(client)

    acc = _mk_account(client)
    uid = acc["id"]
    pid = client.post(f"/v1/ops/users/{uid}/profiles",
                      json={"nickname": "지안"}).json()["id"]

    # 정식(NIR) 기기 + 테스트(웹캠) 기기를 각각 등록 — DeviceSim 이 쓰는 평문 토큰 보관
    nir_token, web_token = "nir-token", "web-token"
    with env["factory"]() as db:
        db.add(models.Device(
            serial="NIR_DEV_1", hw_rev="nir-1",
            factory_token_hash=hashlib.sha256(nir_token.encode()).hexdigest()))
        db.add(models.Device(
            serial="WEB_DEV_1", hw_rev="webcam-proto",
            factory_token_hash=hashlib.sha256(web_token.encode()).hexdigest()))
        db.commit()
        nir_id = db.execute(select(models.Device)
                            .where(models.Device.serial == "NIR_DEV_1")).scalar_one().id
        web_id = db.execute(select(models.Device)
                            .where(models.Device.serial == "WEB_DEV_1")).scalar_one().id

    for dev_id in (nir_id, web_id):
        assert client.post(f"/v1/ops/devices/{dev_id}/bind",
                           json={"user_id": uid, "default_profile_id": pid}
                           ).status_code == 200

    scenario = [("focus", 90), ("blank_stare", 30)]
    nir_sid = DeviceSim(client, "NIR_DEV_1", nir_token).run_session(scenario)["sid"]
    web_sid = DeviceSim(client, "WEB_DEV_1", web_token).run_session(scenario)["sid"]

    # ① 정식 기기 리포트 — 동공(NIR) 없이 재정규화 → 알람 non-null
    nir_report = client.get(f"/v1/ops/sessions/{nir_sid}/report").json()
    assert nir_report["result"]["quality"]["sfi_renormalized"] is True
    assert nir_report["alarm"] is not None
    assert nir_report["alarm"]["code"] == "pupil_missing_on_production"

    # ② 테스트 기기 리포트 — 동공 부재가 전제 → 알람 None
    web_report = client.get(f"/v1/ops/sessions/{web_sid}/report").json()
    assert web_report["alarm"] is None

    # ③ 대시보드 ops.pupil_alarms ≥ 1 (정식 기기 알람 집계)
    d = client.get("/v1/ops/dashboard").json()
    assert d["ops"]["pupil_alarms"] >= 1

    # ④ UI 렌더 — 대시보드/정식 기기 리포트 페이지 200
    assert client.get("/ops/dashboard").status_code == 200
    assert client.get(f"/ops/sessions/{nir_sid}").status_code == 200


def test_subscription_manual_and_webhook(env):
    """수동 부여/변경 + 결제 웹훅 스텁(시크릿 인증·상태 갱신)."""
    client = env["client"]
    _login(client)
    uid = _mk_account(client)["id"]

    # 수동 부여 → 변경
    r = client.post("/v1/ops/subscriptions",
                    json={"user_id": uid, "state": "trial", "months": 2})
    assert r.status_code == 200
    sub_id = r.json()["id"]
    r = client.post(f"/v1/ops/subscriptions/{sub_id}", json={"state": "active"})
    assert r.json()["state"] == "active"

    # 웹훅 — 시크릿 불일치 401
    assert client.post("/v1/billing/webhook",
                       headers={"X-Billing-Secret": "wrong"},
                       json={"event_type": "subscription_renewed",
                             "user_id": uid}).status_code == 401
    # 정상 웹훅 — 같은 (user, product) upsert
    secret = get_settings().billing_webhook_secret
    r = client.post("/v1/billing/webhook",
                    headers={"X-Billing-Secret": secret},
                    json={"event_type": "subscription_renewed",
                          "user_id": uid, "months": 12})
    assert r.status_code == 200 and r.json()["state"] == "active"
    r = client.post("/v1/billing/webhook",
                    headers={"X-Billing-Secret": secret},
                    json={"event_type": "subscription_expired", "user_id": uid})
    assert r.json()["state"] == "expired"
    # 미지의 이벤트 422
    assert client.post("/v1/billing/webhook",
                       headers={"X-Billing-Secret": secret},
                       json={"event_type": "mystery", "user_id": uid}).status_code == 422


def test_app_pull_shares_canonical_report(env):
    """앱 pull — 콘솔과 동일 정본 JSON, 타 계정 차단, 토큰 검증."""
    client = env["client"]
    _login(client)

    acc = _mk_account(client)
    uid, token = acc["id"], acc["app_token"]
    pid = client.post(f"/v1/ops/users/{uid}/profiles",
                      json={"nickname": "지안"}).json()["id"]
    with env["factory"]() as db:
        dev_id = db.execute(select(models.Device)
                            .where(models.Device.serial == SERIAL)).scalar_one().id
    client.post(f"/v1/ops/devices/{dev_id}/bind",
                json={"user_id": uid, "default_profile_id": pid})
    sid = DeviceSim(client, SERIAL, FACTORY_TOKEN).run_session([("focus", 120)])["sid"]

    hdr = {"Authorization": f"Bearer {token}"}
    # 무토큰/오염 토큰 401
    assert client.get("/v1/app/me").status_code == 401
    assert client.get("/v1/app/me",
                      headers={"Authorization": "Bearer nope"}).status_code == 401

    me = client.get("/v1/app/me", headers=hdr).json()
    assert me["user_id"] == uid and me["profiles"][0]["id"] == pid

    lst = client.get(f"/v1/app/profiles/{pid}/reports", headers=hdr).json()
    assert lst["total"] == 1 and lst["reports"][0]["sid"] == sid
    assert lst["reports"][0]["engine"] == "v1.0"

    # 상세 = ops 콘솔 리포트와 동일 정본
    app_detail = client.get(f"/v1/app/reports/{sid}", headers=hdr).json()
    ops_detail = client.get(f"/v1/ops/sessions/{sid}/report").json()
    assert app_detail["result"] == ops_detail["result"]
    assert app_detail["param_set_version"] == ops_detail["param_set_version"]

    # 사용 시간 분해(2026-07-07 결정) — 120초 focus 세션이면 총 2.0분 = 집중 2.0분
    mins = app_detail["result"]["minutes"]
    assert mins["total"] == pytest.approx(2.0, abs=0.1)
    assert mins["focus"] == pytest.approx(2.0, abs=0.1)
    assert mins["off_task"] == pytest.approx(0.0, abs=0.1)

    # 타 계정 토큰으로는 404 (열거 방지)
    other = _mk_account(client, "other2@example.com")
    other_hdr = {"Authorization": f"Bearer {other['app_token']}"}
    assert client.get(f"/v1/app/profiles/{pid}/reports",
                      headers=other_hdr).status_code == 404
    assert client.get(f"/v1/app/reports/{sid}", headers=other_hdr).status_code == 404

    # 리포트 열람 감사로그 (ops 경로)
    audit = client.get("/v1/ops/audit").json()["audit"]
    assert any(a["action"] == "report_viewed" and a["target"] == sid for a in audit)


def test_app_creates_profile_idempotent(env):
    """프로필 정본 입력은 앱(2026-07-07 결정) — 온보딩 재실행에도 중복 없음."""
    client = env["client"]
    _login(client)
    acc = _mk_account(client)
    hdr = {"Authorization": f"Bearer {acc['app_token']}"}

    r1 = client.post("/v1/app/profiles", headers=hdr,
                     json={"nickname": "지안", "birth_date": "2016-03-12"})
    assert r1.status_code == 200 and r1.json()["created"] is True
    pid = r1.json()["id"]

    # 앱이 온보딩을 다시 돌아도 같은 닉네임 → 기존 프로필 반환
    r2 = client.post("/v1/app/profiles", headers=hdr, json={"nickname": "지안"})
    assert r2.json()["id"] == pid and r2.json()["created"] is False

    # 앱 입력이 ops 콘솔에 그대로 연동되어 보인다
    detail = client.get(f"/v1/ops/users/{acc['id']}").json()
    assert [p["nickname"] for p in detail["profiles"]] == ["지안"]

    # 가족 상한 3은 앱 경로에도 동일 적용
    for name in ("둘째", "셋째"):
        assert client.post("/v1/app/profiles", headers=hdr,
                           json={"nickname": name}).status_code == 200
    assert client.post("/v1/app/profiles", headers=hdr,
                       json={"nickname": "넷째"}).status_code == 409

    # 무토큰 401
    assert client.post("/v1/app/profiles", json={"nickname": "x"}).status_code == 401


def test_dashboard_country_drilldown(env):
    """대시보드 개편(2026-07-07): 국가별 구독 → 기간별 → 계정 목록 드릴다운."""
    client = env["client"]
    _login(client)
    secret = get_settings().billing_webhook_secret

    # 한국 2명(2개월/12개월), 미국 1명(12개월), 테스트(국가 없음) 1명
    kr1 = _mk_account(client, "kr1@example.com")["id"]
    kr2 = _mk_account(client, "kr2@example.com")["id"]
    us1 = _mk_account(client, "us1@example.com")["id"]
    tt = _mk_account(client, "test1@example.com")["id"]
    client.post("/v1/billing/webhook", headers={"X-Billing-Secret": secret},
                json={"event_type": "subscription_activated", "user_id": kr1,
                      "months": 2, "country": "KR", "provider": "play"})
    client.post("/v1/billing/webhook", headers={"X-Billing-Secret": secret},
                json={"event_type": "subscription_activated", "user_id": kr2,
                      "months": 12, "country": "kr", "provider": "play"})
    client.post("/v1/billing/webhook", headers={"X-Billing-Secret": secret},
                json={"event_type": "subscription_activated", "user_id": us1,
                      "months": 12, "country": "US", "provider": "play"})
    client.post("/v1/ops/subscriptions", json={"user_id": tt, "months": 4})

    # ① 대시보드 — 국가 카드(KR 2, US 1) + 테스트 카드(1), 테스트는 항상 마지막
    d = client.get("/v1/ops/dashboard").json()
    assert d["subscriptions"]["active_total"] == 4
    cards = {c["country"]: c for c in d["subscriptions"]["countries"]}
    assert cards["KR"]["total"] == 2 and cards["KR"]["by_months"]["2"] == 1 \
        and cards["KR"]["by_months"]["12"] == 1
    assert cards["US"]["total"] == 1
    assert cards["test"]["total"] == 1 and cards["test"]["by_months"]["4"] == 1
    assert d["subscriptions"]["countries"][-1]["country"] == "test"

    # ② 기간 필터 → 계정 목록
    lst = client.get("/v1/ops/subscriptions?country=KR&months=12").json()
    assert [s["email"] for s in lst["subscriptions"]] == ["kr2@example.com"]
    lst = client.get("/v1/ops/subscriptions?country=test").json()
    assert [s["email"] for s in lst["subscriptions"]] == ["test1@example.com"]

    # ③ UI 렌더 — 대시보드/국가 상세/필터 목록
    assert client.get("/ops/dashboard").status_code == 200
    assert client.get("/ops/dashboard/KR").status_code == 200
    assert client.get("/ops/dashboard/test").status_code == 200
    assert client.get(
        "/ops/subscriptions?country=KR&months=12").status_code == 200
    # 없는 국가 → 대시보드로 리다이렉트
    assert client.get("/ops/dashboard/FR",
                      follow_redirects=False).status_code == 302


def test_versions_history(env):
    """버전 이력(2026-07-07) — 채택 1건 = 0.0.0.1 증가, 시드 v1.0 이 첫 버전."""
    client = env["client"]
    _login(client)
    v = client.get("/v1/ops/versions").json()
    assert v["count"] >= 1
    first = v["versions"][-1]  # 최신이 위 → 마지막이 최초
    assert first["version"] == "0.0.0.1"
    assert first["param_set_version"] == "v1.0"
    assert len(first["updated_at"]) == len("2026-07-04-08")  # YYYY-MM-DD-HH
    assert v["current"] is not None
    assert client.get("/ops/versions").status_code == 200


def test_ops_ui_account_pages_render(env):
    """계정 목록/상세/구독 페이지 Jinja 렌더 스모크 — 템플릿 문법 오류 조기 검출."""
    client = env["client"]
    _login(client)
    uid = _mk_account(client)["id"]
    client.post(f"/v1/ops/users/{uid}/profiles", json={"nickname": "지안"})
    assert client.get("/ops/users").status_code == 200
    assert client.get("/ops/users?q=parent").status_code == 200
    assert client.get(f"/ops/users/{uid}").status_code == 200
    assert client.get("/ops/subscriptions").status_code == 200
    # 존재하지 않는 계정 → 목록으로 리다이렉트
    r = client.get("/ops/users/99999", follow_redirects=False)
    assert r.status_code == 302


def test_session_glasses_flag(env):
    """안경 착용 플래그(2026-07-08) — 기기 보고값이 세션에 저장되고 리포트에 노출.
    미보고(None)면 미상으로 남는다."""
    client = env["client"]
    _login(client)
    uid = _mk_account(client, "glasses@example.com")["id"]
    pid = client.post(f"/v1/ops/users/{uid}/profiles",
                      json={"nickname": "안경이"}).json()["id"]
    with env["factory"]() as db:
        dev_id = db.execute(select(models.Device)
                            .where(models.Device.serial == SERIAL)).scalar_one().id
    client.post(f"/v1/ops/devices/{dev_id}/bind",
                json={"user_id": uid, "default_profile_id": pid})

    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    sid_g = sim.run_session([("focus", 120)], glasses=True)["sid"]
    sid_u = sim.run_session([("focus", 120)])["sid"]  # 미보고 → 미상

    rep_g = client.get(f"/v1/ops/sessions/{sid_g}/report").json()
    rep_u = client.get(f"/v1/ops/sessions/{sid_u}/report").json()
    assert rep_g["glasses"] is True
    assert rep_u["glasses"] is None
    assert client.get(f"/ops/sessions/{sid_g}").status_code == 200  # 👓 뱃지 렌더


def test_device_params_pull(env):
    """기기 파라미터 pull(2026-07-08) — 간이 판정·넛지가 펌웨어 업데이트 없이
    진화(채택 param_set)를 따라가는 통로. 기본값·부분집합·인증을 검증한다."""
    client = env["client"]
    # 미인증 → 401
    assert client.get("/v1/devices/params").status_code == 401
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    assert sim.auth().status_code == 200
    p = client.get("/v1/devices/params", headers=sim._headers()).json()
    assert p["param_set_version"] is not None      # 시드 채택본이 잡힌다
    assert p["live"]["gaze"]                       # 간이 판정 부분집합(gaze)만
    assert "blank_stare" not in p["live"]          # 멍때림은 리포트 전용 — 미배포
    # 원장 확정 기본값: 연속 이탈 60초 → 차임, 쿨다운 120초, 세션 최대 3회
    assert p["nudge"] == {"offtask_sec": 60, "cooldown_sec": 120,
                          "max_per_session": 3}
