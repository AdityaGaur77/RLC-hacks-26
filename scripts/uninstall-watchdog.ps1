# Remove the watchdog and stop the collector.

$startup = [Environment]::GetFolderPath("Startup")
$dst = Join-Path $startup "palimpsest-watchdog.cmd"
if (Test-Path $dst) { Remove-Item $dst -Force; Write-Output "removed $dst" }

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "palimpsest.collect" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Output "stopped collector pid $($_.ProcessId)"
    }

Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "watchdog.ps1" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Output "stopped watchdog pid $($_.ProcessId)"
    }
