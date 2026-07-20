"""가짜 기기(device simulator) — S2 계획 §1.7.

웹캠 프로토 tests/synth.py 의 합성 record 생성기를 그대로 재사용해
auth → start → chunk×N → finish 전 과정을 수행한다.
fastapi.testclient.TestClient 와 httpx.Client 모두 같은 인터페이스라 둘 다 받는다.
이후 모든 단계(S3 채점 E2E, S4 프로토 연결 리허설)의 기본 도구.
"""
from __future__ import annotations

import time
import uuid

from app.services.integrity import nan_to_none, records_crc32


def load_synth():
    """합성 record 생성기 — S3 부터 서버가 정본이므로 로컬 tests/synth.py 사용."""
    from tests import synth
    return synth


class DeviceSim:
    def __init__(self, client, serial: str, factory_token: str):
        self.client = client
        self.serial = serial
        self.factory_token = factory_token
        self.token: str | None = None

    # ---------- 공통 ----------

    def _headers(self, nonce: str | None = None, ts: int | None = None) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Nonce": nonce or uuid.uuid4().hex,
            "X-Timestamp": str(ts if ts is not None else int(time.time())),
        }

    # ---------- 단계 ----------

    def auth(self):
        r = self.client.post("/v1/devices/auth",
                             json={"serial": self.serial,
                                   "factory_token": self.factory_token})
        if r.status_code != 200:
            return r
        self.token = r.json()["access_token"]
        return r

    def start(self, mode: str = "DEV-2", duration_sec: int = 120,
              schema_version: str = "4.2", glasses: bool | None = None):
        return self.client.post("/v1/sessions/start", headers=self._headers(), json={
            "firmware_version": "sim-0.1",
            "schema_version": schema_version,
            "session_mode": mode,
            "duration_sec": duration_sec,
            "sample_rate_hz": 10,
            "upload_policy": "AFTER_SESSION_ONLY",
            "raw_video_uploaded": False,
            "research_mode": True,
            "glasses": glasses,   # 안경 착용(기기 감지, None=미상 — 2026-07-08)
        })

    @staticmethod
    def make_chunks(records: list[dict], chunk_size: int = 600) -> list[dict]:
        """records → chunk 요청 본문 목록 (CRC 는 서버와 공유하는 규약으로 계산)."""
        records = nan_to_none(records)
        chunks = []
        for ci, off in enumerate(range(0, len(records), chunk_size)):
            part = records[off:off + chunk_size]
            chunks.append({
                "chunk_index": ci,
                "first_sample_index": part[0]["sample_index"],
                "crc32": records_crc32(part),
                "records": part,
            })
        return chunks

    def send_chunk(self, sid: str, chunk: dict):
        return self.client.post(f"/v1/sessions/{sid}/chunk",
                                headers=self._headers(), json=chunk)

    def finish(self, sid: str, records: list[dict], expected_samples: int | None = None):
        canonical = nan_to_none(sorted(records, key=lambda r: r["sample_index"]))
        return self.client.post(f"/v1/sessions/{sid}/finish", headers=self._headers(), json={
            "expected_samples": expected_samples if expected_samples is not None else len(records),
            "session_crc": records_crc32(canonical),
        })

    # ---------- 편의: E2E 한 방 ----------

    def run_session(self, scenario: list[tuple[str, float]], mode: str = "DEV-2",
                    chunk_size: int = 600, chunk_order: list[int] | None = None,
                    drop_chunks: set[int] = frozenset(),
                    duplicate_chunks: set[int] = frozenset(),
                    glasses: bool | None = None) -> dict:
        """auth(필요시)→start→chunk→finish. 시나리오 총 초 = 세션 duration."""
        if self.token is None:
            r = self.auth()
            assert r.status_code == 200, r.text
        duration = int(sum(sec for _, sec in scenario))
        r = self.start(mode=mode, duration_sec=duration, glasses=glasses)
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        synth = load_synth()
        records = synth.make_records(scenario)
        chunks = self.make_chunks(records, chunk_size)
        order = chunk_order if chunk_order is not None else list(range(len(chunks)))
        responses = []
        for i in order:
            if i in drop_chunks:
                continue
            responses.append(self.send_chunk(sid, chunks[i]))
            if i in duplicate_chunks:
                responses.append(self.send_chunk(sid, chunks[i]))
        fin = self.finish(sid, records, expected_samples=duration * 10)
        return {"sid": sid, "chunks": responses, "finish": fin, "records": records}
