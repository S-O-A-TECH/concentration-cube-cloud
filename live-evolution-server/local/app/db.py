"""SQLite — 이 서버가 소유하는 유일한 저장소 (SPEC-00 §4).

원천 데이터(세션·라벨·param_sets)는 운영 서버 소유 — 여기엔 절대 두지 않는다.
보존 대상: agent_runs(프롬프트·응답 원문·비용 — 연구 기록), proposals(세대 이력의
이 서버측 절반), settings(런타임 설정), loop_state(상태 머신 영속), console_sessions.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from typing import Any

from .config import get_config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL              -- JSON
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gen_id TEXT,
  agent TEXT NOT NULL,
  purpose TEXT NOT NULL,           -- auth_check | propose
  attempt INTEGER DEFAULT 1,
  command TEXT,
  mission TEXT,                    -- MISSION.md 전문 (재현성 — SPEC-04 §4)
  workspace TEXT,
  started_at TEXT,
  ended_at TEXT,
  duration_sec REAL,
  exit_code INTEGER,
  stdout TEXT,
  stderr TEXT,
  cost_usd REAL,
  ok INTEGER,
  error TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  gen_id TEXT PRIMARY KEY,
  created_at TEXT,
  agent TEXT,
  status TEXT,                     -- proposed|registered|passed|rejected|adopted|rolled_back|failed|dismissed
  proposal_json TEXT,              -- proposal.v1 원문
  validation_json TEXT,
  param_set_id TEXT,
  version TEXT,
  report_json TEXT,                -- 성적표 스냅샷 (운영 서버 응답)
  verdict TEXT,
  reject_reason TEXT,
  human_note TEXT
);
CREATE TABLE IF NOT EXISTS loop_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL,
  gen_id TEXT,
  updated_at TEXT,
  data TEXT                        -- JSON: log, workspace, error, ...
);
CREATE TABLE IF NOT EXISTS console_sessions (
  sid TEXT PRIMARY KEY,
  started_at TEXT,
  mode TEXT,
  labeler TEXT,
  protocol TEXT,
  clicks_json TEXT,
  status TEXT,                     -- running|finished|saved|discarded
  saved_at TEXT,
  sync_warning INTEGER DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            cfg = get_config()
            _conn = sqlite3.connect(cfg.db_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(_SCHEMA)
            _conn.commit()
        return _conn


def close() -> None:
    """테스트 전용."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ settings

def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    with _lock:
        c = connect()
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value, ensure_ascii=False)))
        c.commit()


# ------------------------------------------------------------------ agent_runs

def add_agent_run(**kw) -> int:
    cols = ("gen_id", "agent", "purpose", "attempt", "command", "mission", "workspace",
            "started_at", "ended_at", "duration_sec", "exit_code", "stdout", "stderr",
            "cost_usd", "ok", "error")
    vals = [kw.get(c) for c in cols]
    with _lock:
        c = connect()
        cur = c.execute(f"INSERT INTO agent_runs({','.join(cols)}) "
                        f"VALUES({','.join('?' * len(cols))})", vals)
        c.commit()
        return int(cur.lastrowid)


def agent_runs(gen_id: str | None = None, purpose: str | None = None,
               limit: int = 50) -> list[dict]:
    q = "SELECT * FROM agent_runs WHERE 1=1"
    args: list = []
    if gen_id:
        q += " AND gen_id=?"
        args.append(gen_id)
    if purpose:
        q += " AND purpose=?"
        args.append(purpose)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = connect().execute(q, args).fetchall()
    return [dict(r) for r in rows]


def runs_today(purpose: str = "propose") -> int:
    today = dt.date.today().isoformat()
    with _lock:
        row = connect().execute(
            "SELECT COUNT(*) n FROM agent_runs WHERE purpose=? AND started_at LIKE ?",
            (purpose, f"{today}%")).fetchone()
    return int(row["n"])


# ------------------------------------------------------------------ proposals (세대 이력의 이 서버측 절반)

def upsert_proposal(gen_id: str, **kw) -> None:
    cols = ("created_at", "agent", "status", "proposal_json", "validation_json",
            "param_set_id", "version", "report_json", "verdict", "reject_reason", "human_note")
    with _lock:   # 읽기-수정-쓰기를 한 임계구역으로 (lost update 방지 — 리뷰 MAJOR 1)
        cur = get_proposal(gen_id) or {}
        cur.update({k: v for k, v in kw.items() if v is not None})
        c = connect()
        c.execute(
            f"INSERT INTO proposals(gen_id,{','.join(cols)}) VALUES(?,{','.join('?' * len(cols))}) "
            f"ON CONFLICT(gen_id) DO UPDATE SET " + ",".join(f"{c2}=excluded.{c2}" for c2 in cols),
            [gen_id] + [cur.get(c2) for c2 in cols])
        c.commit()


def get_proposal(gen_id: str) -> dict | None:
    with _lock:
        row = connect().execute("SELECT * FROM proposals WHERE gen_id=?", (gen_id,)).fetchone()
    return dict(row) if row else None


def list_proposals(limit: int = 100) -> list[dict]:
    with _lock:
        rows = connect().execute(
            "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def consecutive_rejects() -> int:
    """최근 세대부터 연속 REJECTED 수 — 3 이상이면 '레벨 2 검토' 표시 (SPEC-06 §7)."""
    n = 0
    for p in list_proposals(limit=20):
        if p["status"] in ("dismissed", "failed"):
            continue
        if p["status"] == "rejected":
            n += 1
        else:
            break
    return n


# ------------------------------------------------------------------ loop_state

def save_loop_state(state: str, gen_id: str | None, data: dict) -> None:
    with _lock:
        c = connect()
        c.execute("INSERT INTO loop_state(id,state,gen_id,updated_at,data) VALUES(1,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET state=excluded.state, gen_id=excluded.gen_id, "
                  "updated_at=excluded.updated_at, data=excluded.data",
                  (state, gen_id, _now(), json.dumps(data, ensure_ascii=False)))
        c.commit()


def load_loop_state() -> dict | None:
    with _lock:
        row = connect().execute("SELECT * FROM loop_state WHERE id=1").fetchone()
    if not row:
        return None
    return {"state": row["state"], "gen_id": row["gen_id"],
            "updated_at": row["updated_at"], "data": json.loads(row["data"] or "{}")}


# ------------------------------------------------------------------ console_sessions

def save_console_session(sid: str, **kw) -> None:
    cols = ("started_at", "mode", "labeler", "protocol", "clicks_json", "status",
            "saved_at", "sync_warning")
    with _lock:   # upsert_proposal 과 동일 — 읽기-수정-쓰기 원자화
        cur_row = get_console_session(sid) or {}
        cur_row.update({k: v for k, v in kw.items() if v is not None})
        c = connect()
        c.execute(
            f"INSERT INTO console_sessions(sid,{','.join(cols)}) VALUES(?,{','.join('?' * len(cols))}) "
            f"ON CONFLICT(sid) DO UPDATE SET " + ",".join(f"{c2}=excluded.{c2}" for c2 in cols),
            [sid] + [cur_row.get(c2) for c2 in cols])
        c.commit()


def get_console_session(sid: str) -> dict | None:
    with _lock:
        row = connect().execute("SELECT * FROM console_sessions WHERE sid=?", (sid,)).fetchone()
    return dict(row) if row else None
