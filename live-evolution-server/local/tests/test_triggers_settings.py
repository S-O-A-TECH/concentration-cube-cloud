"""E6 DoD: 트리거 계산 / 설정 API / 아카이브 필터·제외 토글 (앱 API 경유)."""
from app.triggers import decide_trigger


def test_decide_trigger_pure():
    assert decide_trigger(labeled_now=10, labels_at_last_gen=5, n=5) is True
    assert decide_trigger(labeled_now=9, labels_at_last_gen=5, n=5) is False
    assert decide_trigger(labeled_now=5, labels_at_last_gen=5, n=5) is False
    assert decide_trigger(labeled_now=3, labels_at_last_gen=5, n=5) is False  # 감소해도 미발화


def test_settings_roundtrip(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["defaults"]["trigger_n"] == 5
    r = client.post("/api/settings", json={"trigger_n": 7, "auto_propose": True})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["values"]["trigger_n"] == 7
    r = client.post("/api/settings", json={"hack": 1})
    assert r.status_code == 400


def test_dashboard_shape(client):
    d = client.get("/api/dashboard").json()
    assert d["ops"]["ok"] is True
    assert d["ops"]["overview"]["active_param_set"]["version"] == "v1.0"
    assert d["loop"]["state"] == "IDLE"
    assert d["today"]["runs"] == 0


def test_archive_filters_and_exclude_toggle(client):
    d = client.get("/api/archive").json()
    assert d["stats"]["labeled"] >= 10
    sid = next(s["sid"] for s in d["sessions"] if s["split"] == "train" and s["labeled"])

    # 사유 없는 제외는 거부
    assert client.post(f"/api/archive/{sid}/exclude", json={"reason": " "}).status_code == 400
    assert client.post(f"/api/archive/{sid}/exclude",
                       json={"reason": "리허설 제외"}).status_code == 200
    d2 = client.get("/api/archive?include_excluded=true").json()
    row = next(s for s in d2["sessions"] if s["sid"] == sid)
    assert row["excluded"] is True
    # 제외 세션은 include_excluded=false 필터에서 사라진다 (평가 대상 제외의 UI 반영)
    d3 = client.get("/api/archive?include_excluded=false").json()
    assert all(s["sid"] != sid for s in d3["sessions"])
    assert client.post(f"/api/archive/{sid}/restore",
                       json={"reason": "복원"}).status_code == 200

    # split 필터
    d4 = client.get("/api/archive?split=holdout").json()
    assert d4["sessions"] and all(s["split"] == "holdout" for s in d4["sessions"])

    # 상세 = 판정 타임라인 + 정답지 + 세대별 runs
    det = client.get(f"/api/archive/{sid}").json()
    assert det["result"]["timeline"]
    assert det["labels"]["segments"]
    assert det["runs"] and det["runs"][0]["param_set_version"] == "v1.0"
    assert "classifier_inputs" not in det   # 화면에는 내려보내지 않는다


def test_mistakes_api(client):
    d = client.get("/api/mistakes").json()
    assert d["bins_total"] > 0
    assert d["mistakes"], "시드 데이터에는 의도된 오답이 있어야 한다"
    m = d["mistakes"][0]
    assert {"session_id", "t0", "t1", "truth", "predicted", "features_summary"} <= set(m)


def test_generations_api_empty_then_shape(client):
    d = client.get("/api/generations").json()
    assert [p["version"] for p in d["param_sets"]] == ["v1.0"]
    assert d["param_sets"][0]["local"] is None   # 시드는 이 서버가 만들지 않았다
