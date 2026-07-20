"""mock 운영 서버 — /v1/evolution/* 를 SPEC-02 §1.3 계약대로 구현한 개발·테스트 대역.

실서버(server/, 다른 팀이 Docker 로 병행 개발 중)의 S5 가 완성되면 이 mock 은 끄고
.env 의 SERVER_URL 만 실서버로 돌리면 된다 — live-evolution 코드 변경 0.

'정적 JSON' 이 아니라 '기능형' mock 이다:
  - 세션: 지정 폴더(웹캠 프로토 data/sessions, 시드 demo_sessions)의 실물
           record.parquet + meta.json 을 읽는다
  - mistakes / evaluate: live-evolution 과 같은 분류 코어(app.loop.sim.classify_core)로
           실제 계산한다 — 게이트가 측정하는 것과 같은 지표를 같은 코드로 (SPEC-06 §3)
  - split: sha1(sid) 결정적 배정 (~25% holdout)
  - 라벨 불변(재-POST 409) / 제외≠삭제 / promote 는 passed+confirm 만 (409 이중 방어)

상태는 JSON 파일 하나에 영속 — 원천 데이터 흉내일 뿐이므로 단순함이 정의다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import threading
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

_LOCAL_ROOT = Path(__file__).resolve().parents[2]
if str(_LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(_LOCAL_ROOT))

from app.loop.sim import classify_core as core  # noqa: E402

MOCK_VERSION = "mock-ops-0.1"
ENGINE_VERSION = "mock-S3"
GATE_RULE = ("holdout 모든 상태 sens·spec 이 baseline 대비 -2%p 이내 저하 & 표적 개선 "
             "(mock — 실서버 S5 의 게이트 정의가 정본)")
MAX_DROP = 0.02
MISTAKES_CAP = 300

_now = lambda: dt.datetime.now().astimezone().isoformat(timespec="seconds")


def split_of(sid: str) -> str:
    """결정적 train/holdout 배정 — sha1 마지막 바이트 %4==3 → holdout (~25%)."""
    return "holdout" if hashlib.sha1(sid.encode()).digest()[-1] % 4 == 3 else "train"


# ------------------------------------------------------------------ 상태

class MockState:
    def __init__(self, path: Path, seed_params: dict):
        self.path = path
        self._lock = threading.RLock()
        if path.exists():
            self.d = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.d = {
                "labels": {}, "excluded": {}, "param_sets": [], "reports": {},
                "jobs": [], "runs": {}, "audit": [], "seq": 0,
            }
            self.d["param_sets"].append({
                "id": "ps1", "version": "v1.0", "status": "adopted", "active": True,
                "origin": "seed", "agent_name": None, "rationale": "시드 파라미터 (웹캠 프로토 default.json)",
                "parent_id": None, "json_params": seed_params, "created_at": _now(),
            })
            self.d["seq"] = 1
            self.save()

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    def audit(self, action: str, target: str, detail: str = ""):
        self.d["audit"].append({"at": _now(), "action": action, "target": target,
                                "detail": detail})

    def active_ps(self) -> dict:
        return next(p for p in self.d["param_sets"] if p.get("active"))

    def ps_by_id(self, pid: str) -> dict | None:
        return next((p for p in self.d["param_sets"] if p["id"] == pid), None)


# ------------------------------------------------------------------ 세션 소스 (폴더 스캔)

class SessionStore:
    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        self._df_cache: dict[tuple, pd.DataFrame] = {}

    def scan(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for d in self.dirs:
            if not d.exists():
                continue
            for sub in sorted(d.iterdir()):
                meta_p = sub / "meta.json"
                rec_p = sub / "record.parquet"
                if not (sub.is_dir() and meta_p.exists() and rec_p.exists()):
                    continue
                try:
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                out[meta.get("sid", sub.name)] = {"meta": meta, "dir": sub}
        return out

    def record_df(self, entry: dict) -> pd.DataFrame:
        p = entry["dir"] / "record.parquet"
        key = (str(p), p.stat().st_mtime_ns)
        if key not in self._df_cache:
            self._df_cache.clear()  # 세션 수가 적다 — 단순 무효화로 충분
            self._df_cache[key] = pd.read_parquet(p)
        return self._df_cache[key]


# ------------------------------------------------------------------ 채점 (선택 — 웹캠 focus_scoring 재사용)

def _load_scorer(webcam_root: Path | None):
    if not webcam_root:
        return None
    root = Path(webcam_root)
    if not (root / "p0_webcam" / "focus_scoring" / "__init__.py").exists():
        return None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from p0_webcam.focus_scoring import score  # noqa
        return score
    except Exception:
        return None


# ------------------------------------------------------------------ 앱 팩토리

class LabelsReq(BaseModel):
    labeler: str
    method: str
    protocol: str = ""
    segments: list[dict]


class ReasonReq(BaseModel):
    reason: str = ""


class ParamSetReq(BaseModel):
    json_params: dict
    origin: str = "agent"
    agent_name: str | None = None
    rationale: str = ""
    parent_id: str | None = None
    version: str | None = None


class PromoteReq(BaseModel):
    confirm: bool = False


def create_app(sessions_dirs: list[Path], state_path: Path,
               token: str = "dev-evolution-token",
               webcam_root: Path | None = None) -> FastAPI:
    seed_params = json.loads(
        (Path(__file__).parent / "params_v1.json").read_text(encoding="utf-8"))
    state = MockState(Path(state_path), seed_params)
    store = SessionStore(sessions_dirs)
    scorer = _load_scorer(webcam_root)
    app = FastAPI(title="mock ops server (/v1/evolution/*)")

    def auth(request: Request):
        if request.headers.get("X-Evolution-Token") != token:
            raise HTTPException(401, "invalid X-Evolution-Token")

    # ------------------------------------------------------------ 내부 도우미

    def _label(sid: str) -> dict | None:
        return state.d["labels"].get(sid)

    def _is_excluded(sid: str) -> bool:
        return bool(state.d["excluded"].get(sid, {}).get("excluded"))

    def _session_view(sid: str, entry: dict) -> dict:
        lb = _label(sid)
        return {
            "sid": sid,
            "started_at": entry["meta"].get("started_at"),
            "mode": entry["meta"].get("mode"),
            "device_id": entry["meta"].get("device_id", "WEBCAM_PROTO_001"),
            "duration_sec": entry["meta"].get("duration_sec"),
            "labeled": lb is not None,
            "label": ({"method": lb["method"], "protocol": lb.get("protocol"),
                       "labeler": lb.get("labeler"),
                       "label_sec": round(sum(s["t1"] - s["t0"] for s in lb["segments"]), 1)}
                      if lb else None),
            "split": split_of(sid),
            "excluded": _is_excluded(sid),
            "excluded_reason": state.d["excluded"].get(sid, {}).get("reason"),
            "runs_count": len(state.d["runs"].get(sid, [])),
        }

    def _research_sessions(split: str | None = None) -> list[tuple[str, dict]]:
        """평가·오답노트 대상: 실시간 라벨 + 비제외 (retrospective 는 기본 제외 — SPEC-05 §4)."""
        out = []
        for sid, entry in store.scan().items():
            lb = _label(sid)
            if not lb or lb["method"] != "realtime_instructed" or _is_excluded(sid):
                continue
            if split and split_of(sid) != split:
                continue
            out.append((sid, entry))
        return sorted(out)

    def _eval_split(params: dict, split: str) -> dict:
        sessions = []
        for sid, entry in _research_sessions(split):
            df = store.record_df(entry)
            sessions.append({"df": df, "segments": _label(sid)["segments"]})
        return core.eval_sessions(sessions, params)

    def _session_per_state(df: pd.DataFrame, segments: list, params: dict) -> dict:
        m = core.eval_sessions([{"df": df, "segments": segments}], params)["per_state"]
        return {st: {"sens": v["sens"], "spec": v["spec"]} for st, v in m.items()}

    def _run_entry(sid: str, entry: dict, ps: dict) -> dict:
        df = store.record_df(entry)
        lb = _label(sid)
        sfi = None
        if scorer is not None:
            try:
                sfi = scorer(df, ps["json_params"]).get("sfi")
            except Exception:
                sfi = None
        return {
            "param_set_id": ps["id"], "param_set_version": ps["version"],
            "engine_version": ENGINE_VERSION, "sfi": sfi,
            "per_state": (_session_per_state(df, lb["segments"], ps["json_params"])
                          if lb else None),
            "run_at": _now(),
        }

    def _infer_targets(changed_keys: list[str]) -> list[str]:
        targets = []
        for k in changed_keys:
            if k.startswith("blank_stare.") and "blank_stare" not in targets:
                targets.append("blank_stare")
            if k.startswith("gaze.") and "off_task" not in targets:
                targets.append("off_task")
        return targets

    def _flat(d: dict, prefix: str = "") -> dict:
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(_flat(v, f"{prefix}{k}."))
            else:
                out[f"{prefix}{k}"] = v
        return out

    # ------------------------------------------------------------ 엔드포인트

    @app.get("/health")
    def health():
        return {"ok": True, "db": True, "redis": True, "storage": True,
                "version": MOCK_VERSION, "mock": True}

    @app.get("/v1/evolution/overview")
    def overview(_=Depends(auth)):
        all_sessions = store.scan()
        realtime = [sid for sid in all_sessions
                    if (_label(sid) or {}).get("method") == "realtime_instructed"
                    and not _is_excluded(sid)]
        retro = [sid for sid in all_sessions
                 if (_label(sid) or {}).get("method") == "retrospective"]
        active = state.active_ps()
        return {
            "sessions_total": len(all_sessions),
            "labeled_realtime": len(realtime),
            "labeled_retrospective": len(retro),
            "train_labeled": len([s for s in realtime if split_of(s) == "train"]),
            "holdout_labeled": len([s for s in realtime if split_of(s) == "holdout"]),
            "active_param_set": {"id": active["id"], "version": active["version"]},
            "gate_rule": GATE_RULE,
            "last_session_at": max((e["meta"].get("started_at") or ""
                                    for e in all_sessions.values()), default=None),
            "last_label_at": max((lb.get("saved_at") or "" for lb in state.d["labels"].values()),
                                 default=None),
        }

    @app.get("/v1/evolution/sessions")
    def sessions(labeled: bool | None = None, split: str | None = None,
                 mode: str | None = None, include_excluded: bool = False,
                 _=Depends(auth)):
        out = []
        for sid, entry in sorted(store.scan().items(), reverse=True):
            v = _session_view(sid, entry)
            if labeled is True:
                lb = _label(sid)
                if not lb or lb["method"] != "realtime_instructed" or v["excluded"]:
                    continue
            if labeled is False and v["labeled"]:
                continue
            if not include_excluded and v["excluded"]:
                continue
            if split and v["split"] != split:
                continue
            if mode and v["mode"] != mode:
                continue
            out.append(v)
        return {"sessions": out}

    def _entry_or_404(sid: str) -> dict:
        entry = store.scan().get(sid)
        if not entry:
            raise HTTPException(404, f"no such session: {sid}")
        return entry

    @app.get("/v1/evolution/sessions/{sid}/detail")
    def session_detail(sid: str, _=Depends(auth)):
        entry = _entry_or_404(sid)
        df = store.record_df(entry)
        active = state.active_ps()
        states = core.classify_states(df, active["json_params"])
        t_sec = df["t_ms"].to_numpy() / 1000.0
        timeline = []
        for i, st in enumerate(states):
            if timeline and timeline[-1]["state"] == st:
                timeline[-1]["t1"] = round(float(t_sec[i]), 1)
            else:
                timeline.append({"t0": round(float(t_sec[i]), 1),
                                 "t1": round(float(t_sec[i]), 1), "state": str(st)})
        return {
            "sid": sid,
            "meta": entry["meta"],
            "view": _session_view(sid, entry),
            "result": {"param_set_version": active["version"], "timeline": timeline},
            "labels": _label(sid),
            "classifier_inputs": {c: df[c].tolist() for c in core.CLASSIFIER_COLUMNS},
        }

    @app.get("/v1/evolution/sessions/{sid}/labels")
    def labels_get(sid: str, _=Depends(auth)):
        _entry_or_404(sid)
        return _label(sid) or {"segments": []}

    @app.post("/v1/evolution/sessions/{sid}/labels")
    def labels_post(sid: str, req: LabelsReq, _=Depends(auth)):
        # FastAPI 동기 라우트는 스레드풀에서 돈다 — 모든 상태 변이는 state._lock 아래로
        # (리뷰 MAJOR 3: check-then-write 경합)
        _entry_or_404(sid)
        if req.method not in ("realtime_instructed", "retrospective"):
            raise HTTPException(400, f"unknown method: {req.method}")
        allowed = set(core.LABEL_STATES)
        for s in req.segments:
            if s.get("label") not in allowed:
                raise HTTPException(400, f"label must be one of {sorted(allowed)}")
            if not (isinstance(s.get("t0"), (int, float)) and isinstance(s.get("t1"), (int, float))
                    and s["t0"] < s["t1"]):
                raise HTTPException(400, "segment must satisfy t0 < t1")
        with state._lock:
            if sid in state.d["labels"]:
                # ★불변: 세션당 1회 — 사후 수정 금지의 서버측 강제 (SPEC-05 §4)
                raise HTTPException(409, "labels already exist for this session (immutable)")
            state.d["labels"][sid] = {"labeler": req.labeler, "method": req.method,
                                      "protocol": req.protocol,
                                      "segments": req.segments, "saved_at": _now()}
            state.audit("labels_post", sid, f"method={req.method} n={len(req.segments)}")
            state.save()
        return {"ok": True, "sid": sid, "n_segments": len(req.segments)}

    @app.post("/v1/evolution/sessions/{sid}/exclude")
    def exclude(sid: str, req: ReasonReq, _=Depends(auth)):
        _entry_or_404(sid)
        if not req.reason.strip():
            raise HTTPException(400, "reason required")
        with state._lock:
            state.d["excluded"][sid] = {"excluded": True, "reason": req.reason, "at": _now()}
            state.audit("exclude", sid, req.reason)
            state.save()
        return {"ok": True}

    @app.post("/v1/evolution/sessions/{sid}/restore")
    def restore(sid: str, req: ReasonReq, _=Depends(auth)):
        _entry_or_404(sid)
        if not req.reason.strip():
            raise HTTPException(400, "reason required")
        with state._lock:
            state.d["excluded"][sid] = {"excluded": False, "reason": req.reason, "at": _now()}
            state.audit("restore", sid, req.reason)
            state.save()
        return {"ok": True}

    @app.get("/v1/evolution/sessions/{sid}/runs")
    def session_runs(sid: str, _=Depends(auth)):
        entry = _entry_or_404(sid)
        with state._lock:
            runs = state.d["runs"].get(sid, [])
            if not runs:
                runs = [_run_entry(sid, entry, state.active_ps())]
                state.d["runs"][sid] = runs
                state.save()
        return {"runs": runs}

    @app.get("/v1/evolution/mistakes")
    def mistakes(param_set_id: str | None = None, _=Depends(auth)):
        ps = state.ps_by_id(param_set_id) if param_set_id else state.active_ps()
        if not ps:
            raise HTTPException(404, "no such param_set")
        params = ps["json_params"]
        rows = []
        pairs = []
        for sid, entry in _research_sessions("train"):
            df = store.record_df(entry)
            lb = _label(sid)
            t_sec = df["t_ms"].to_numpy() / 1000.0
            total = float(t_sec[-1]) if len(df) else 0.0
            truth = core.bins_from_segments(lb["segments"], total)
            pred = core.predicted_bins(df, params)
            for b, (t, p) in enumerate(zip(truth, pred)):
                if t is None or p == "invalid":
                    continue
                pairs.append((t, p))
                if t != p:
                    m = (t_sec >= b * core.BIN_SEC) & (t_sec < (b + 1) * core.BIN_SEC)
                    rows.append({
                        "session_id": sid,
                        "t0": b * core.BIN_SEC, "t1": (b + 1) * core.BIN_SEC,
                        "truth": t, "predicted": p,
                        "features_summary": {
                            f: round(float(df.loc[m, f].mean()), 4)
                            for f in ("gaze_on_page_prob", "gaze_dispersion_1s",
                                      "saccade_count_1s")},
                    })
        metrics = core.confusion_and_metrics(pairs)
        return {
            "param_set_id": ps["id"], "param_set_version": ps["version"],
            "bins_total": metrics["n_bins"],
            "mistakes": rows[:MISTAKES_CAP],
            "confusion": metrics["confusion"],
            "per_state": metrics["per_state"],
        }

    @app.get("/v1/evolution/param_sets")
    def param_sets(_=Depends(auth)):
        out = []
        for ps in state.d["param_sets"]:
            rep = state.d["reports"].get(ps["id"])
            out.append({**ps, "gate_passed": rep["gate"]["passed"] if rep else None})
        return {"param_sets": out}

    @app.post("/v1/evolution/param_sets")
    def param_sets_post(req: ParamSetReq, _=Depends(auth)):
        with state._lock:   # seq 증가·목록 추가가 원자적 (리뷰 MAJOR 3: 중복 ID 방지)
            active = state.active_ps()
            if set(_flat(req.json_params)) != set(_flat(active["json_params"])):
                raise HTTPException(400, "json_params keyset mismatch with active param_set")
            state.d["seq"] += 1
            n = state.d["seq"]
            pid = f"ps{n}"
            version = req.version or f"v1.{n - 1}-gen{n - 1}"
            state.d["param_sets"].append({
                "id": pid, "version": version, "status": "candidate", "active": False,
                "origin": req.origin, "agent_name": req.agent_name,
                "rationale": req.rationale, "parent_id": req.parent_id or active["id"],
                "json_params": req.json_params, "created_at": _now(),
            })
            state.audit("param_set_register", pid, f"origin={req.origin}")
            state.save()
        return {"id": pid, "version": version, "status": "candidate"}

    @app.post("/v1/evolution/param_sets/{pid}/evaluate")
    def evaluate(pid: str, _=Depends(auth)):
        with state._lock:
            ps = state.ps_by_id(pid)
            if not ps:
                raise HTTPException(404, "no such param_set")
            baseline = state.active_ps()
            job_id = f"job{len(state.d['jobs']) + 1}"
            job = {"job_id": job_id, "kind": "evaluate", "param_set_id": pid,
                   "status": "running", "created_at": _now(), "progress": 0}
            state.d["jobs"].insert(0, job)
        # 채점 계산은 락 밖 (수백 ms — 조회 요청을 막지 않는다), 결과 반영은 다시 락 안
        report: dict = {"param_set_id": pid, "baseline_id": baseline["id"],
                        "evaluated_at": _now()}
        try:
            targets = _infer_targets(sorted(
                k for k, v in _flat(ps["json_params"]).items()
                if _flat(baseline["json_params"]).get(k) != v))
            for split in ("train", "holdout"):
                before = _eval_split(baseline["json_params"], split)["per_state"]
                after = _eval_split(ps["json_params"], split)["per_state"]
                report[split] = {"per_state": {
                    st: {"sens": {"before": before[st]["sens"], "after": after[st]["sens"]},
                         "spec": {"before": before[st]["spec"], "after": after[st]["spec"]}}
                    for st in core.LABEL_STATES}}
            # 게이트 (mock 정의 — 실서버 S5 가 정본): holdout 전 상태 -2%p 이내 & 표적 개선
            notes = []
            passed = True
            hold = report["holdout"]["per_state"]
            for st, m in hold.items():
                for metric in ("sens", "spec"):
                    b, a = m[metric]["before"], m[metric]["after"]
                    if b is not None and a is not None and a < b - MAX_DROP:
                        passed = False
                        notes.append(f"{st}.{metric} 저하 {round((b - a) * 100, 1)}%p (> 2%p)")
            check_states = targets or list(core.LABEL_STATES)
            improved = any(
                hold[st][metric]["before"] is not None and hold[st][metric]["after"] is not None
                and hold[st][metric]["after"] > hold[st][metric]["before"] + 1e-9
                for st in check_states for metric in ("sens", "spec"))
            if not improved:
                passed = False
                notes.append(f"표적 상태({'/'.join(check_states)}) 개선 없음")
            report["gate"] = {"rule": GATE_RULE, "passed": passed,
                              "targets": check_states,
                              "notes": "; ".join(notes) if notes else "통과"}
            with state._lock:
                ps["status"] = "passed" if passed else "rejected"
                state.d["reports"][pid] = report
                job["status"] = "done"
                job["progress"] = 100
        except Exception as e:
            with state._lock:
                job["status"] = "failed"
                job["error"] = str(e)
        with state._lock:
            state.audit("evaluate", pid, f"passed={ps.get('status')}")
            state.save()
        return {"job_id": job_id, "status": job["status"]}

    @app.get("/v1/evolution/param_sets/{pid}/report")
    def report(pid: str, _=Depends(auth)):
        rep = state.d["reports"].get(pid)
        if not rep:
            raise HTTPException(404, "not evaluated yet")
        return rep

    @app.post("/v1/evolution/param_sets/{pid}/promote")
    def promote(pid: str, req: PromoteReq, _=Depends(auth)):
        with state._lock:
            ps = state.ps_by_id(pid)
            if not ps:
                raise HTTPException(404, "no such param_set")
            if not req.confirm:
                raise HTTPException(400, "confirm:true required (사람 버튼)")
            if ps["status"] != "passed":
                # 이중 방어 — 게이트 미통과 채택 시도는 서버가 409 (E5 DoD)
                raise HTTPException(409, f"gate not passed (status={ps['status']})")
            prev = state.active_ps()
            prev["active"] = False
            ps["active"] = True
            ps["status"] = "adopted"
            job_id = f"job{len(state.d['jobs']) + 1}"
            job = {"job_id": job_id, "kind": "rescore", "param_set_id": pid,
                   "status": "running", "created_at": _now(), "progress": 0}
            state.d["jobs"].insert(0, job)
        # 전 세션 재채점(느린 계산)은 락 밖 — runs 반영·저장은 다시 락 안
        n = 0
        new_runs: list[tuple[str, dict]] = []
        for sid, entry in sorted(store.scan().items()):
            try:
                new_runs.append((sid, _run_entry(sid, entry, ps)))
                n += 1
            except Exception:
                pass
        with state._lock:
            for sid, run in new_runs:
                state.d["runs"].setdefault(sid, []).append(run)
            job["status"] = "done"
            job["progress"] = 100
            job["n_rescored"] = n
            state.audit("promote", pid, f"prev={prev['id']} rescored={n} (성적표 스냅샷 보존)")
            state.save()
        return {"ok": True, "id": pid, "version": ps["version"], "rescore_job": job_id,
                "n_rescored": n}

    @app.post("/v1/evolution/param_sets/{pid}/reject")
    def reject(pid: str, req: ReasonReq, _=Depends(auth)):
        with state._lock:
            ps = state.ps_by_id(pid)
            if not ps:
                raise HTTPException(404, "no such param_set")
            if ps["status"] in ("adopted",) and ps.get("active"):
                raise HTTPException(409, "active param_set 은 reject 불가 — rollback 을 쓰세요")
            ps["status"] = "rejected"
            state.audit("reject", pid, req.reason)
            state.save()
        return {"ok": True}

    @app.post("/v1/evolution/param_sets/{pid}/rollback")
    def rollback(pid: str, req: ReasonReq, _=Depends(auth)):
        with state._lock:
            ps = state.ps_by_id(pid)
            if not ps:
                raise HTTPException(404, "no such param_set")
            if not ps.get("active"):
                raise HTTPException(409, "active 세대만 롤백할 수 있습니다")
            parent = state.ps_by_id(ps.get("parent_id") or "")
            if not parent:
                raise HTTPException(409, "롤백할 이전 세대가 없습니다")
            ps["active"] = False
            ps["status"] = "rolled_back"
            parent["active"] = True
            parent["status"] = "adopted"
            state.audit("rollback", pid, f"→ {parent['id']} ({req.reason})")
            state.save()
        return {"ok": True, "active": {"id": parent["id"], "version": parent["version"]}}

    @app.get("/v1/evolution/jobs")
    def jobs(_=Depends(auth)):
        return {"jobs": state.d["jobs"][:50]}

    return app
