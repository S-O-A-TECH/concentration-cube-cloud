"""세션 원본 저장소 — 로컬 파일시스템 v0 (SPEC-01 §3).

STORAGE_ROOT/sessions/<sid>/
├─ record.parquet   # finish 후 불변
├─ chunks/          # 수신 중 임시 (finish 검증 후 병합·삭제)
│  ├─ manifest.json # {chunk_index: {crc32, first_sample_index, count}}
│  └─ chunk_00000.parquet ...
└─ meta.json

클라우드에서 Object Storage 로 바꿀 때 이 파일만 교체한다 (인터페이스 유지).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

from app.focus_scoring import records as rec


class SessionStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ---------- 경로 ----------

    def session_dir(self, sid: str) -> Path:
        return self.root / "sessions" / str(sid)

    def chunks_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "chunks"

    def record_path(self, sid: str) -> Path:
        return self.session_dir(sid) / "record.parquet"

    # ---------- meta ----------

    def read_meta(self, sid: str) -> dict:
        p = self.session_dir(sid) / "meta.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def update_meta(self, sid: str, patch: dict) -> dict:
        meta = self.read_meta(sid)
        meta.update(patch)
        d = self.session_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    # ---------- chunk ----------
    # chunk 별 사이드카(chunk_NNNNN.json) 방식 — 공유 파일 read-modify-write 없음
    # (동시 chunk 업로드 경합 대비: 각 요청은 자기 파일만 원자적으로 쓴다)

    def _sidecar_path(self, sid: str, chunk_index: int) -> Path:
        return self.chunks_dir(sid) / f"chunk_{chunk_index:05d}.json"

    def read_manifest(self, sid: str) -> dict:
        """{chunk_index(str): {crc32, first_sample_index, count}} — 사이드카 스캔으로 도출."""
        cdir = self.chunks_dir(sid)
        manifest: dict = {}
        for p in sorted(cdir.glob("chunk_*.json")):
            try:
                manifest[str(int(p.stem.split("_")[1]))] = json.loads(
                    p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError, IndexError):
                continue                      # 찢어진 사이드카는 미완 chunk 로 간주
        return manifest

    def write_chunk(self, sid: str, chunk_index: int, records: list[dict],
                    crc32: int, first_sample_index: int) -> dict:
        """chunk parquet + 사이드카(원자적 tmp→replace) 저장. 갱신된 manifest 반환."""
        cdir = self.chunks_dir(sid)
        cdir.mkdir(parents=True, exist_ok=True)
        rec.write_parquet(records, cdir / f"chunk_{chunk_index:05d}.parquet")
        sidecar = self._sidecar_path(sid, chunk_index)
        tmp = sidecar.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "crc32": crc32,
            "first_sample_index": first_sample_index,
            "count": len(records),
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(sidecar)                  # 사이드카 존재 = chunk 커밋 완료
        return self.read_manifest(sid)

    def merge_chunks(self, sid: str) -> pd.DataFrame:
        """커밋된(사이드카 있는) chunk 만 sample_index 순으로 병합 (중복은 첫 값 유지)."""
        cdir = self.chunks_dir(sid)
        parts = []
        for idx in sorted(int(k) for k in self.read_manifest(sid)):
            p = cdir / f"chunk_{idx:05d}.parquet"
            if p.exists():
                parts.append(rec.read_parquet(p))
        if not parts:
            return rec.to_dataframe([])
        df = pd.concat(parts, ignore_index=True)
        df = (df.drop_duplicates(subset="sample_index", keep="first")
                .sort_values("sample_index").reset_index(drop=True))
        return df

    # ---------- finalize ----------

    def finalize(self, sid: str, df: pd.DataFrame, meta_patch: dict | None = None) -> Path:
        """record.parquet 확정 (이후 불변).

        가드(C1): 확정본이 이미 있는데 빈 df 로 덮어쓰려는 호출은 무시 —
        어떤 경합·재시도도 비어있지 않은 확정본을 파괴할 수 없다.
        chunks/ 정리는 여기서 하지 않는다 — DB 커밋 후 cleanup_chunks() 로.
        """
        path = self.record_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(df) == 0 and self.has_record(sid):
            return path
        rec.write_parquet(df, path)
        if meta_patch:
            self.update_meta(sid, meta_patch)
        return path

    def cleanup_chunks(self, sid: str) -> None:
        """chunk 임시 폴더 정리 — 반드시 finish 트랜잭션 커밋 이후에 호출."""
        shutil.rmtree(self.chunks_dir(sid), ignore_errors=True)

    def has_record(self, sid: str) -> bool:
        return self.record_path(sid).exists()
