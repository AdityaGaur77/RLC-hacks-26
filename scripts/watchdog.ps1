# Keeps the Palimpsest collector alive.
#
# Archive depth is the one thing that cannot be recovered after the fact: a gap
# in observation is indistinguishable from a period in which nothing changed.
# This runs at logon and on a short interval, and is idempotent -- if a healthy
# collector is already running it does nothing.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "archive\collector.pid"
$LogFile = Join-Path $Root "archive\watchdog.log"

function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

# Is a collector already up? Match on the command line, not just the pid file:
# a recycled pid belonging to some other python process must not count.
$running = $false
if (Test-Path $PidFile) {
    $storedPid = (Get-Content $PidFile -Raw).Trim()
    if ($storedPid) {
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $storedPid" -ErrorAction Stop
            if ($proc -and $proc.CommandLine -match "palimpsest.collect") { $running = $true }
        } catch { $running = $false }
    }
}

if (-not $running) {
    $any = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match "palimpsest.collect" }
    if ($any) {
        $running = $true
        $any[0].ProcessId | Out-File -FilePath $PidFile -Encoding utf8
        Write-Log "adopted orphaned collector pid $($any[0].ProcessId)"
    }
}

if ($running) { exit 0 }

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Log "ERROR python not found on PATH"; exit 1 }

# A sweep costs ~15 minutes and ~660 requests across seven portals, so a
# two-hour cycle sits near 50 requests per portal per hour -- comfortably inside
# an unauthenticated budget. The tighter interval is not about volume: it is what
# lets the archive catch a value that changes and changes back between sweeps,
# which a slower cadence would silently miss.
$p = Start-Process -FilePath $python `
    -ArgumentList "-m","palimpsest.collect","--interval","2",
                  "--db","archive\palimpsest.db","--log","archive\collector.log" `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru

$p.Id | Out-File -FilePath $PidFile -Encoding utf8
Write-Log "started collector pid $($p.Id)"
