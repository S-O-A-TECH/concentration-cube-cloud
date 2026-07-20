# RUNBOOK — 백업 복구 리허설 (S7 §1.5)

**목표:** `backups/<날짜>/` 백업만으로 **빈 환경에서 서버를 되살리고**, 기존 세션의
result 가 정상 조회됨을 확인한다. 리허설 환경은 `-p cube-restore` 프로젝트로
본 서버와 볼륨·네트워크·포트가 완전히 분리된다 (본 서버는 계속 가동).

## 0. 전제

- 백업 실행됨: `powershell -File scripts\backup.ps1` → `backups/<stamp>/cube.dump` + `storage_snapshot/`
- 아래 명령은 전부 `server\local` 에서 실행. `$B = "backups\<stamp>"`

## 1. 빈 복구 환경 기동 (postgres/redis/api — 포트 15433/16380/18100)

```powershell
docker compose -p cube-restore -f docker-compose.yml -f docker-compose.restore.yml up -d postgres redis
```

## 2. DB 복원

```powershell
# 덤프 파일을 컨테이너로 복사 후 복원 (바이너리 파이프 회피 — Windows 안전 경로)
docker compose -p cube-restore -f docker-compose.yml -f docker-compose.restore.yml cp "$B\cube.dump" postgres:/tmp/cube.dump
docker compose -p cube-restore -f docker-compose.yml -f docker-compose.restore.yml exec -T postgres pg_restore -U cube -d cube --no-owner /tmp/cube.dump
# (pg_restore 경고 0~수 건은 정상 — ERROR 만 확인)
```

## 3. STORAGE 복원

```powershell
docker compose -p cube-restore -f docker-compose.yml -f docker-compose.restore.yml up -d api
docker compose -p cube-restore cp "$B\storage_snapshot\." api:/data/storage
```

## 4. 검증 (리허설 완료 기준 = S7 DoD)

```powershell
# health 전항목 true
curl http://127.0.0.1:18100/health
# 운영 콘솔 로그인 → 세션 목록 → 기존 세션 리포트 조회 (브라우저: http://127.0.0.1:18100/ops)
# 또는 자동 검증:
.venv\Scripts\python tools\verify_restore.py backups\<stamp>
```

`tools/verify_restore.py` 는 복구 환경(18100)에 ops 로그인 → 세션 목록 →
첫 complete 세션의 리포트(result)를 조회해 sfi 존재를 확인한다.

## 5. 리허설 환경 정리

```powershell
docker compose -p cube-restore -f docker-compose.yml -f docker-compose.restore.yml down -v
```

## 장애 시나리오별 요약

| 상황 | 절차 |
|:---|:---|
| DB 볼륨 유실 | §1~4 를 본 프로젝트(`-p` 없이)에 적용 — down -v 후 복원 |
| storage 볼륨 유실 | §3 만 (DB 는 그대로) — parquet 은 불변이라 최신 백업으로 충분 |
| 특정 세션 parquet 만 손상 | `backups/storage_mirror/sessions/<sid>/` 에서 해당 폴더만 cp |
| 백업 실패 감지 | `backups/LAST_BACKUP_FAILED` 파일 존재 여부 (스케줄러가 매일 갱신) |
