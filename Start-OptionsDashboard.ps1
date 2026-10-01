param(
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [switch]$NoBrowser,
    [string]$DataDirectory = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not $DataDirectory) { $DataDirectory = Join-Path $projectRoot '.local' }
$DataDirectory = [System.IO.Path]::GetFullPath($DataDirectory)
$url = 'http://127.0.0.1:' + $Port
$mutexBytes = [System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))
$mutexKey = ([System.BitConverter]::ToString($mutexBytes)).Replace('-', '')
$launchMutex = New-Object System.Threading.Mutex($false, ('Local\OptionsDashboard-' + $mutexKey))
$ownsMutex = $false

function Read-Health {
    try { return Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 2 } catch { return $null }
}
function Open-Dashboard {
    if (-not $NoBrowser) { Start-Process $url }
}
try {
    $ownsMutex = $launchMutex.WaitOne(0)
    if (-not $ownsMutex) { throw 'OptionsDashboard is already starting. Please wait a moment.' }
    $health = Read-Health
    if ($health -and $health.application -eq 'OptionsDashboard') {
        if ($health.workspace -ne $projectRoot -or $health.data_directory -ne $DataDirectory) {
            throw 'Another OptionsDashboard folder is using this port. Stop that instance before starting this one.'
        }
        Write-Host 'OptionsDashboard is already running. Opening it.'
        Open-Dashboard
        exit 0
    }
    # During migration, reuse the original running terminal instead of starting
    # a second scheduler against the same broker/account.
    $legacy = $null
    try { $legacy = Invoke-RestMethod -Uri ($url + '/api/bootstrap') -TimeoutSec 2 } catch {}
    if ($legacy -and $legacy.state.strategy.configured) {
        Write-Host 'Your original terminal is still running. Opening that session.'
        Write-Host 'After it is closed, this launcher will start OptionsDashboard and import its latest saved history.'
        Open-Dashboard
        exit 0
    }
    $probe = New-Object System.Net.Sockets.TcpClient
    try { $probe.Connect('127.0.0.1', $Port); $occupied = $true } catch { $occupied = $false } finally { $probe.Dispose() }
    if ($occupied) { throw "Port $Port is used by another application. OptionsDashboard was not started." }

    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host 'Preparing Python 3.12 (first run only)...'
        if ($uvCommand) {
            & $uvCommand.Source venv --python 3.12 .venv
        } else {
            $pyCommand = Get-Command py -ErrorAction SilentlyContinue
            if (-not $pyCommand) { throw 'Install Python 3.12 from python.org or uv from astral.sh, then double-click Start-OptionsDashboard.cmd again.' }
            & $pyCommand.Source -3.12 -m venv .venv
        }
        if ($LASTEXITCODE -ne 0) { throw 'Python setup failed.' }
    }
    & $python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"'
    if ($LASTEXITCODE -ne 0) { throw 'The local Python environment needs to be recreated with Python 3.12.' }
    $lockFile = Join-Path $projectRoot 'requirements.lock'
    $stampFile = Join-Path $projectRoot '.venv\.requirements-hash'
    $lockHash = (Get-FileHash -LiteralPath $lockFile -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $stampFile) { (Get-Content -LiteralPath $stampFile -Raw).Trim() } else { '' }
    if ($installedHash -ne $lockHash) {
        Write-Host 'Installing the tested dependencies...'
        if ($uvCommand) { & $uvCommand.Source pip sync --python $python $lockFile }
        else { & $python -m pip install -r $lockFile }
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your internet connection and try again.' }
        Set-Content -LiteralPath $stampFile -Value $lockHash -Encoding ASCII
    }
    New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null
    & $python (Join-Path $projectRoot 'scripts\migrate_state.py') $DataDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Local history migration needs attention. The server was not started.' }
    $env:TERMINAL_DATA_DIR = $DataDirectory
    $env:NEO_SDK_PATH = Join-Path $projectRoot 'vendor\kotak_neo'
    $server = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app:app','--host','127.0.0.1','--port',"$Port",'--no-access-log') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DataDirectory 'server-out.log') -RedirectStandardError (Join-Path $DataDirectory 'server-err.log') -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $health = Read-Health
        if ($health -and $health.workspace -eq $projectRoot -and $health.data_directory -eq $DataDirectory) { $ready = $true; break }
        if ($server.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw "The dashboard did not start. Check $DataDirectory\server-err.log." }
    Write-Host "OptionsDashboard is ready at $url"
    Write-Host 'Connect with a fresh TOTP each day. Keep the computer awake for scheduled entries and exits.'
    Open-Dashboard
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($ownsMutex) { $launchMutex.ReleaseMutex() }
    $launchMutex.Dispose()
}
