"""진화 루프 상태 머신 (SPEC-02 §1).

IDLE → COLLECT → PROPOSE → REVIEW_DIFF → REGISTERED → EVALUATING → PASSED/REJECTED
                     └(검증 실패·타임아웃)→ FAILED
ADOPTED 로 가는 문은 성적표 화면의 [채택] 버튼 하나뿐 (여기서는 promote 를 부르지 않는다
— routes 가 사람 클릭을 받아 ops.promote 후 mark_adopted 를 호출).

- 동시 1개 (전역 락) — 두 번째 실행 요청은 거부.
- 상태는 매 전이마다 SQLite loop_state 에 영속 — 브라우저 이탈/재접속에도 이어진다.
- 자동 제안 모드(auto)는 REVIEW_DIFF 를 자동 통과해 EVALUATING 까지 가되,
  PASSED/REJECTED 에서 반드시 정지한다 (E6 §2 — 테스트로 증명).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import traceback

from .. import db
from ..agents import get_adapter
from ..config import get_config
from ..server_client import OpsError, get_ops
from . import evidence as evidence_mod
from . import workspace as workspace_mod
from .validate import validate_proposal

STATES = ("IDLE", "COLLECT", "PROPOSE", "REVIEW_DIFF", "REGISTERED",
          "EVALUATING", "PASSED", "REJECTED", "FAILED")
MIN_LABELED_SESSIONS = 10          # 라벨 부족 가드 (E4 §1.10)
MAX_ATTEMPTS = 3                   # 재시도 포함 (SPEC-06 §4)
POLL_SEC = 5.0                     # evaluate 잡 폴링 (SPEC-02 §1)
POLL_TIMEOUT_SEC = 1800.0

_now = lambda: dt.datetime.now().astimezone().isoformat(timespec="seconds")


class EvolutionLoop:
    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self.state = "IDLE"
        self.gen_id: str | None = None
        self.data: dict = {}
        self._resume()

    # ------------------------------------------------------------- 영속

    def _persist(self):
        db.save_loop_state(self.state, self.gen_id, self.data)

    def _resume(self):
        saved = db.load_loop_state()
        if not saved:
            self._persist()
            return
        self.state, self.gen_id, self.data = saved["state"], saved["gen_id"], saved["data"]
        if self.state in ("COLLECT", "PROPOSE"):
            # 실행 스레드는 재시작으로 사라졌다 — 정직하게 FAILED 처리 (원문은 agent_runs 에)
            self._log("서버 재시작으로 세대 실행이 중단되었습니다 — FAILED 처리")
            self._set("FAILED", error="서버 재시작으로 중단")
            if self.gen_id:
                db.upsert_proposal(self.gen_id, status="failed",
                                   reject_reason="서버 재시작으로 중단")
        elif self.state == "REGISTERED":
            self._start_thread(self._evaluate_worker)
        elif self.state == "EVALUATING":
            self._log("서버 재시작 — evaluate 폴링 재개")
            self._start_thread(self._poll_worker)

    def _set(self, state: str, **data_updates):
        with self._lock:
            self.state = state
            self.data.update(data_updates)
            self.data["state_changed_at"] = _now()
            self._persist()

    def _log(self, msg: str):
        with self._lock:
            log = self.data.setdefault("log", [])
            log.append({"t": _now(), "msg": msg})
            del log[:-500]
            self._persist()

    # ------------------------------------------------------------- 조회

    def status(self) -> dict:
        with self._lock:
            prop = db.get_proposal(self.gen_id) if self.gen_id else None
            return {
                "state": self.state,
                "gen_id": self.gen_id,
                "data": {k: v for k, v in self.data.items() if k != "log"},
                "log": self.data.get("log", [])[-120:],
                "proposal": json.loads(prop["proposal_json"]) if prop and prop["proposal_json"] else None,
                "validation": json.loads(prop["validation_json"]) if prop and prop["validation_json"] else None,
                "param_set_id": prop["param_set_id"] if prop else None,
                "version": prop["version"] if prop else None,
                "report": json.loads(prop["report_json"]) if prop and prop["report_json"] else None,
                "consecutive_rejects": db.consecutive_rejects(),
            }

    def can_run(self) -> tuple[bool, str]:
        if self.state not in ("IDLE",):
            return False, f"진행 중인 세대가 있습니다 (상태: {self.state}) — 완료/정리 후 실행하세요."
        limit = db.get_setting("daily_run_limit", get_config().daily_run_limit)
        if db.runs_today("propose") >= int(limit):
            return False, f"일일 실행 상한({limit}회)에 도달했습니다."
        try:
            ov = get_ops().overview()
        except OpsError as e:
            return False, e.user_msg
        labeled = int(ov.get("labeled_realtime", 0))
        if labeled < MIN_LABELED_SESSIONS:
            return False, (f"라벨 세션이 부족합니다 ({labeled}/{MIN_LABELED_SESSIONS}) — "
                           "검증 세션 콘솔에서 정답지를 더 만들어 주세요.")
        return True, ""

    def estimate(self) -> dict:
        runs = [r for r in db.agent_runs(purpose="propose", limit=20) if r["duration_sec"]][:5]
        if not runs:
            return {"n": 0, "avg_duration_sec": None, "avg_cost_usd": None}
        costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
        return {"n": len(runs),
                "avg_duration_sec": round(sum(r["duration_sec"] for r in runs) / len(runs), 1),
                "avg_cost_usd": round(sum(costs) / len(costs), 4) if costs else None}

    # ------------------------------------------------------------- 실행

    def run(self, agent_name: str | None = None, auto: bool = False) -> dict:
        # 네트워크가 포함된 가드는 락 밖에서 — UI 의 status() 폴링을 막지 않는다
        ok, reason = self.can_run()
        if not ok:
            raise RuntimeError(reason)
        with self._lock:
            if self.state != "IDLE":   # 가드 통과 후 경합 재확인 (동시 1개 원칙)
                raise RuntimeError(f"진행 중인 세대가 있습니다 (상태: {self.state})")
            agent_name = agent_name or db.get_setting("agent_default", get_config().agent_default)
            gen_seq = len([p for p in db.list_proposals(limit=1000)]) + 1
            self.gen_id = dt.datetime.now().strftime("gen%Y%m%d_%H%M%S")
            self.data = {"agent": agent_name, "auto": auto, "gen_seq": gen_seq, "log": []}
            self._set("COLLECT")
        db.upsert_proposal(self.gen_id, created_at=_now(), agent=agent_name, status="collecting")
        self._log(f"세대 {self.gen_id} 시작 (에이전트: {agent_name}, 자동모드: {auto})")
        self._start_thread(self._run_worker)
        return {"gen_id": self.gen_id}

    def _start_thread(self, target):
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def _run_worker(self):
        try:
            self._collect_and_propose()
        except Exception:
            err = traceback.format_exc(limit=5)
            self._log(f"예기치 못한 오류: {err.splitlines()[-1]}")
            self._set("FAILED", error=err)
            if self.gen_id:
                db.upsert_proposal(self.gen_id, status="failed", reject_reason=err.splitlines()[-1])

    def _collect_and_propose(self):
        ops = get_ops()
        cfg = get_config()
        self._log("오답노트·현황 수집 중 (운영 서버 GET)…")
        try:
            ev = evidence_mod.build_evidence(ops)
        except OpsError as e:
            self._log(f"수집 실패: {e.user_msg}")
            self._set("FAILED", error=e.user_msg)
            db.upsert_proposal(self.gen_id, status="failed", reject_reason=e.user_msg)
            return
        meta = ev["meta"]
        self.data["active_param_set"] = meta["active_param_set"]
        self.data["failure_modes"] = meta["failure_modes"]
        self._log(f"Evidence: train {meta['n_train_sessions']}세션 / bins {meta['n_train_bins']} / "
                  f"실패 모드 {meta['failure_modes']} / 활성 {meta['active_param_set']['version']}")

        agent_name = self.data["agent"]
        adapter = get_adapter(agent_name)
        auth = adapter.check_auth()   # 실행 직전 항상 재확인 (SPEC-01 §2)
        if not auth.ok:
            msg = f"에이전트({agent_name}) 인증 실패: {auth.detail}"
            self._log(msg)
            self._set("FAILED", error=msg)
            db.upsert_proposal(self.gen_id, status="failed", reject_reason=msg)
            return

        timeout = float(db.get_setting("agent_timeout_sec", cfg.agent_timeout_sec))
        current_params = ev["files"]["current_params.json"]
        tb = ev["files"]["targets_bounds.json"]
        history = ev["files"]["history.json"]["generations"]
        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._set("PROPOSE", attempt=attempt)
            self._log(f"에이전트 실행 {attempt}/{MAX_ATTEMPTS} (타임아웃 {int(timeout)}s)…")
            ws = workspace_mod.create_workspace(self.gen_id, ev, feedback=feedback)
            self.data["workspace"] = str(ws)
            started = _now()
            out = adapter.propose(ws, timeout)
            mission = (ws / "MISSION.md").read_text(encoding="utf-8")
            run_id = db.add_agent_run(
                gen_id=self.gen_id, agent=agent_name, purpose="propose", attempt=attempt,
                command=out.command, mission=mission, workspace=str(ws),
                started_at=started, ended_at=_now(), duration_sec=round(out.duration_sec, 1),
                exit_code=out.exit_code, stdout=out.stdout[-200_000:],
                stderr=out.stderr[-50_000:], cost_usd=out.cost_usd,
                ok=1 if out.ok else 0, error=out.error)
            self.data.setdefault("agent_run_ids", []).append(run_id)
            if not out.ok:
                self._log(f"에이전트 실패: {out.error}")
                feedback = f"- 이전 시도가 실패했다: {out.error}\n- output/proposal.json 파일 저장을 잊지 마라."
                continue
            self._log(f"제안 수거 ({out.proposal_source}) — 4중 검증 중…")
            vr = validate_proposal(out.proposal, current_params, tb, history, workspace=ws)
            db.upsert_proposal(self.gen_id,
                               proposal_json=json.dumps(out.proposal, ensure_ascii=False),
                               validation_json=json.dumps(vr.to_dict(), ensure_ascii=False))
            if vr.ok:
                for w in vr.warnings:
                    self._log(f"경고: {w}")
                self._log(f"검증 통과 (4단계 재계산 대조 포함) — 변경 {len(out.proposal['changes'])}건")
                db.upsert_proposal(self.gen_id, status="proposed")
                self._set("REVIEW_DIFF")
                if self.data.get("auto"):
                    self._log("자동 제안 모드 — diff 검토를 건너뛰고 후보 등록 진행 "
                              "(채택은 언제나 수동)")
                    if self._claim_registration():
                        self._register_inner()
                return
            self._log(f"검증 실패 (단계 {vr.stage}): " + " / ".join(vr.errors[:4]))
            feedback = ("- 직전 제안이 검증 " + str(vr.stage) + "단계에서 기각되었다:\n"
                        + "\n".join(f"  * {e}" for e in vr.errors[:8])
                        + "\n- 위 사유를 전부 해소한 제안을 다시 제출하라.")
        msg = f"{MAX_ATTEMPTS}회 시도 모두 실패 — 세대 FAILED (원문은 agent_runs 에 보존)"
        self._log(msg)
        self._set("FAILED", error=msg)
        db.upsert_proposal(self.gen_id, status="failed", reject_reason=msg)

    # ------------------------------------------------------------- 등록·시험

    def _claim_registration(self) -> bool:
        """등록 시작권 원자적 선점 — TOCTOU 이중 등록 봉쇄 (리뷰 CRITICAL 2).

        상태 검사와 선점 표시를 같은 임계구역에서 수행한다. 선점 성공한 호출자만
        _register_inner 를 실행할 수 있다."""
        with self._lock:
            if self.state != "REVIEW_DIFF" or self.data.get("registering"):
                return False
            self.data["registering"] = True
            self._persist()
            return True

    def register(self) -> dict:
        if not self._claim_registration():
            raise RuntimeError(
                f"후보 등록을 시작할 수 없습니다 (상태: {self.state}"
                f"{', 이미 등록 진행 중' if self.data.get('registering') else ''})")
        self._start_thread(self._register_worker)
        return {"gen_id": self.gen_id}

    def _register_worker(self):
        try:
            self._register_inner()
        except Exception:
            err = traceback.format_exc(limit=5)
            self._set("FAILED", error=err)
            db.upsert_proposal(self.gen_id, status="failed", reject_reason=err.splitlines()[-1])

    def _register_inner(self):
        # 전제: 호출 전 _claim_registration() 으로 선점 완료 (register 또는 auto 경로)
        ops = get_ops()
        prop_row = db.get_proposal(self.gen_id)
        proposal = json.loads(prop_row["proposal_json"])
        gen_seq = self.data.get("gen_seq", 1)
        version = f"v1.{gen_seq}-gen{gen_seq}"
        parent = self.data.get("active_param_set", {}).get("id")
        self._log(f"후보 등록: POST param_sets ({version}, parent={parent})")
        try:
            res = ops.param_sets_post(
                json_params=proposal["new_params"], origin="agent",
                agent_name=self.data.get("agent", "?"),
                rationale=proposal.get("rationale", ""), parent_id=parent, version=version)
        except OpsError as e:
            self._log(f"등록 실패: {e.user_msg}")
            self._set("FAILED", error=e.user_msg)
            db.upsert_proposal(self.gen_id, status="failed", reject_reason=e.user_msg)
            return
        pid = res["id"]
        db.upsert_proposal(self.gen_id, status="registered", param_set_id=pid,
                           version=res.get("version", version))
        self._set("REGISTERED", param_set_id=pid)
        self._evaluate_worker()

    def _evaluate_worker(self):
        ops = get_ops()
        pid = self.data.get("param_set_id") or (db.get_proposal(self.gen_id) or {}).get("param_set_id")
        if not pid:
            self._set("FAILED", error="param_set_id 없음")
            return
        self._log("evaluate 요청 (train+holdout 재채점 → 게이트 판정은 운영 서버가)…")
        try:
            job = ops.evaluate(pid)
        except OpsError as e:
            self._log(f"evaluate 실패: {e.user_msg}")
            self._set("FAILED", error=e.user_msg)
            db.upsert_proposal(self.gen_id, status="failed", reject_reason=e.user_msg)
            return
        self._set("EVALUATING", job_id=job.get("job_id"))
        self._poll_worker()

    def _poll_worker(self):
        ops = get_ops()
        pid = self.data.get("param_set_id") or (db.get_proposal(self.gen_id) or {}).get("param_set_id")
        job_id = self.data.get("job_id")
        t0 = time.monotonic()
        while True:
            if time.monotonic() - t0 > POLL_TIMEOUT_SEC:
                self._log("evaluate 잡 대기 시간 초과")
                self._set("FAILED", error="evaluate 잡 대기 시간 초과")
                db.upsert_proposal(self.gen_id, status="failed", reject_reason="evaluate 타임아웃")
                return
            try:
                jobs = ops.jobs()
            except OpsError as e:
                self._log(f"잡 조회 실패(재시도 예정): {e.user_msg}")
                time.sleep(POLL_SEC)
                continue
            mine = [j for j in jobs if j.get("job_id") == job_id or
                    (j.get("param_set_id") == pid and j.get("kind") == "evaluate")]
            st = mine[0]["status"] if mine else None
            if st in ("done", None):
                break
            if st == "failed":
                msg = mine[0].get("error", "evaluate 잡 실패")
                self._log(f"evaluate 실패: {msg}")
                self._set("FAILED", error=msg)
                db.upsert_proposal(self.gen_id, status="failed", reject_reason=msg)
                return
            time.sleep(POLL_SEC)
        try:
            report = ops.report(pid)
        except OpsError as e:
            self._log(f"성적표 조회 실패: {e.user_msg}")
            self._set("FAILED", error=e.user_msg)
            return
        passed = bool(report.get("gate", {}).get("passed"))
        verdict = "passed" if passed else "rejected"
        db.upsert_proposal(self.gen_id, status=verdict, verdict=verdict,
                           report_json=json.dumps(report, ensure_ascii=False),
                           reject_reason=None if passed else report.get("gate", {}).get("notes"))
        self._log(f"게이트 판정: {'통과 — 성적표 검토 후 [채택] 가능' if passed else '기각'}")
        self._set("PASSED" if passed else "REJECTED")
        # 자동 모드도 여기서 반드시 정지 — ADOPTED 로 가는 문은 사람의 [채택] 버튼뿐.

    # ------------------------------------------------------------- 사람의 결정

    def dismiss(self, note: str = "") -> None:
        """폐기/기각 확정/실패 정리 → IDLE. (REVIEW_DIFF 폐기 포함)"""
        with self._lock:
            if self.state not in ("REVIEW_DIFF", "PASSED", "REJECTED", "FAILED"):
                raise RuntimeError(f"현재 상태({self.state})에서는 정리할 수 없습니다")
            if self.gen_id:
                status = {"REVIEW_DIFF": "dismissed", "PASSED": "passed",
                          "REJECTED": "rejected", "FAILED": "failed"}[self.state]
                db.upsert_proposal(self.gen_id, status=status, human_note=note or None)
            self._log("세대 정리 → IDLE")
            self._set("IDLE")

    def mark_adopted(self, param_set_id: str) -> None:
        """promote 성공 후 routes 가 호출 — 세대 기록 + 루프 IDLE 복귀."""
        with self._lock:
            for p in db.list_proposals(limit=50):
                if p["param_set_id"] == param_set_id:
                    db.upsert_proposal(p["gen_id"], status="adopted", verdict="adopted")
                    break
            if self.state == "PASSED":
                self._log("채택 완료 — 다음 세대 준비 (새 오답노트는 새 기준으로 다시 계산됨)")
                self._set("IDLE")

    def mark_rolled_back(self, param_set_id: str) -> None:
        for p in db.list_proposals(limit=100):
            if p["param_set_id"] == param_set_id:
                db.upsert_proposal(p["gen_id"], status="rolled_back")
                break


_loop: EvolutionLoop | None = None
_loop_lock = threading.Lock()


def get_loop() -> EvolutionLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = EvolutionLoop()
        return _loop


def reset_loop() -> None:
    """테스트 전용."""
    global _loop
    with _loop_lock:
        _loop = None
