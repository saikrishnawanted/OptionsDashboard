param([ValidateRange(1024,65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
try {
    $url = 'http://127.0.0.1:' + $Port
    $health = Invoke-RestMethod ($url + '/api/health') -TimeoutSec 3
    if ($health.application -ne 'OptionsDashboard' -or $health.workspace -ne $PSScriptRoot) { throw 'This port belongs to another application or folder.' }
    $state = (Invoke-RestMethod ($url + '/api/bootstrap') -TimeoutSec 3).state
    if ($state.strategy.open) { throw 'TBS still has an active session or open legs. Use Exit TBS and verify its orders before stopping the server.' }
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen | Select-Object -First 1
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if ($processInfo.CommandLine -notmatch 'uvicorn app:app') { throw 'The server process could not be verified.' }
    Stop-Process -Id $listener.OwningProcess
    Write-Host 'OptionsDashboard stopped.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
