# Used by start-planner.bat / start-city.bat: make sure THIS launcher window owns the planner server.
# If a planner (python / uv / hnplan) from an earlier window is still listening on the port, stop it.
# Exit code 0 = port free, 1 = port used by a different program (left alone).
param([int]$Port = 8000)
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $conn) { exit 0 }
foreach ($c in $conn) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    if ($p.ProcessName -notmatch '^(python|pythonw|python3\d*|hnplan|uv)$') {
        Write-Host "  Port $Port is used by another program: $($p.ProcessName) (PID $($p.Id))."
        exit 1
    }
    # also stop the uv.exe that launched it, so nothing restarts or lingers
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).ParentProcessId
    Write-Host "  A planner from an earlier window is still running on port $Port (PID $($p.Id)) - stopping it ..."
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    if ($parent) {
        $pp = Get-Process -Id $parent -ErrorAction SilentlyContinue
        if ($pp -and $pp.ProcessName -match '^(uv|hnplan)$') { Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue }
    }
}
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { exit 0 }
    Start-Sleep -Milliseconds 250
}
Write-Host "  Port $Port is still busy."
exit 1
