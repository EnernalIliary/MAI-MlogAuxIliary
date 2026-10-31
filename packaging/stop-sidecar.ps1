# Kill the logicd sidecar (the window started by 启动逻辑后端.bat), if any.
# py -3 launches python.exe as a child, so matching python.exe command lines
# covers both "python tools\logicd.py" and "py -3 sidecar\logicd.py".

$ErrorActionPreference = 'Continue'
$found = 0
$procs = @()
try {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop)
} catch {
    Write-Host ('[fail] cannot list processes: ' + $_.Exception.Message)
    exit 1
}
foreach ($p in $procs) {
    if ($p.CommandLine -and ($p.CommandLine -like '*logicd.py*')) {
        try {
            Stop-Process -Id $p.ProcessId -Force
            Write-Host ('[ok] killed python pid ' + $p.ProcessId) -ForegroundColor Green
            $found++
        } catch {
            Write-Host ('[warn] pid ' + $p.ProcessId + ': ' + $_.Exception.Message)
        }
    }
}
if ($found -eq 0) { Write-Host '[ok] no logicd sidecar is running' }
