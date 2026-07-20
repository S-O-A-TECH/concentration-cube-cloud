"""무결성 규약 — chunk/세션 CRC 와 결측 구간 계산 (phase2 §2.3, SPEC-02 §2.3).

CRC 계약 (기기와 서버가 공유하는 정의):
  1) record 의 NaN 은 전송 전 null(None) 로 정규화한다 — nan_to_none()
  2) canonical payload = json.dumps(records, separators=(",", ":"),
     sort_keys=True, ensure_ascii=False) 의 UTF-8 바이트
  3) CRC32 = zlib.crc32(payload) 의 unsigned 값 (0..2^32-1)
세션 CRC = 전 record 를 sample_index 오름차순으로 정렬한 배열에 같은 규약 적용.
device_sim(테스트 기기)·웹캠 프로토(S4)가 이 함수를 그대로 import 한다.
"""
from __future__ import annotations

import json
import math
import zlib


def nan_to_none(records: list[dict]) -> list[dict]:
    """float NaN → None (JSON 전송·CRC 정규화). 원본은 건드리지 않는다."""
    out = []
    for r in records:
        out.append({k: (None if isinstance(v, float) and math.isnan(v) else v)
                    for k, v in r.items()})
    return out


def df_to_canonical_records(df) -> list[dict]:
    """DataFrame → CRC 규약용 canonical records.

    parquet/pandas 왕복 후의 numpy 스칼라를 python 원형으로 되돌린다 —
    float64 는 비트 보존이므로 repr(json.dumps) 결과가 기기 측과 일치한다.
    """
    import numpy as np
    import pandas as pd

    out = []
    for row in df.itertuples(index=False):
        d = row._asdict()
        conv = {}
        for k, v in d.items():
            if isinstance(v, float) and math.isnan(v):
                conv[k] = None
            elif v is pd.NaT or v is None:
                conv[k] = None
            elif isinstance(v, (np.bool_, bool)):
                conv[k] = bool(v)
            elif isinstance(v, (np.integer, int)):
                conv[k] = int(v)
            elif isinstance(v, (np.floating,)):
                fv = float(v)
                conv[k] = None if math.isnan(fv) else fv
            else:
                conv[k] = v
        out.append(conv)
    return out


def records_crc32(records: list[dict]) -> int:
    payload = json.dumps(records, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF


def missing_ranges(present_indices: list[int], expected_samples: int) -> list[list[int]]:
    """기대 인덱스 1..expected_samples 대비 결측 구간 [[start, end], ...] (양끝 포함)."""
    present = set(present_indices)
    ranges: list[list[int]] = []
    run_start = None
    for i in range(1, expected_samples + 1):
        if i not in present:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            ranges.append([run_start, i - 1])
            run_start = None
    if run_start is not None:
        ranges.append([run_start, expected_samples])
    return ranges
