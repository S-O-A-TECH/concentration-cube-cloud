# 백업 스크립트 — S7 §1.4 (pg_dump + STORAGE 증분 복사)
# Windows PowerShell 5.1 호환 (작업 스케줄러 기본) — 바이너리는 전부 docker cp 로만 이동
# (PowerShell '>' 리다이렉트는 바이너리를 텍스트로 오염시키므로 금지).
# 실행: powershell -NoProfile -File scripts\backup.ps1   (server\local 에서)
# 작업 스케줄러 등록(일 1회 03:00):
#   schtasks /Create /TN "concentration-cube-backup" /SC DAILY /ST 03:00 `
#     /TR "powershell -NoProfile -File D:\codedprograms\androidapp\concentration_app\server\local\scripts\backup.ps1"
$ErrorActionPreference = "Continue"   # 네이티브 stderr(docker 진행 로그)로 중단되지 않게
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PATH = "C:\Program Files\Docker\Docker\resources\bin;" + $env:PATH

$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$dest = Join-Path $root "backups\$stamp"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

function Fail($msg) {
    Set-Content (Join-Path $root "backups\LAST_BACKUP_FAILED") "$stamp $msg"
    Write-Host "backup FAILED: $msg"
    exit 1
}

# 1) PostgreSQL 덤프 — 컨테이너 안에서 파일로 만들고 docker cp 로 꺼낸다
$out = docker compose exec -T postgres pg_dump -U cube -d cube -Fc -f /tmp/cube.dump 2>&1
if ($LASTEXITCODE -ne 0) { Fail ("pg_dump: " + (($out | Select-Object -Last 3) -join " | ")) }
$out = docker compose cp postgres:/tmp/cube.dump "$dest\cube.dump" 2>&1
if ($LASTEXITCODE -ne 0) { Fail ("dump copy: " + (($out | Select-Object -Last 3) -join " | ")) }
docker compose exec -T postgres rm -f /tmp/cube.dump 2>&1 | Out-Null

# 2) 세션 원본(STORAGE) 스냅샷 — parquet 불변이므로 증분 미러에도 /E /XO 로 반영.
#    /MIR 금지 (삭제 전파 방지 — SPEC-01 §5)
$out = docker compose cp api:/data/storage "$dest\storage_snapshot" 2>&1
if ($LASTEXITCODE -ne 0) { Fail ("storage copy: " + (($out | Select-Object -Last 3) -join " | ")) }

robocopy "$dest\storage_snapshot" "$root\backups\storage_mirror" /E /XO /NFL /NDL /NJH | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "robocopy ($LASTEXITCODE)" }

# 덤프 무결성 최소 확인 (custom format 매직 "PGDMP")
$head = [System.IO.File]::ReadAllBytes("$dest\cube.dump")[0..4]
if ([System.Text.Encoding]::ASCII.GetString($head) -ne "PGDMP") { Fail "dump magic mismatch" }

Remove-Item -Path (Join-Path $root "backups\LAST_BACKUP_FAILED") -ErrorAction SilentlyContinue
Set-Content (Join-Path $root "backups\LAST_BACKUP_OK") "$stamp"
Write-Host "backup OK -> $dest"
exit 0
