param(
    [string]$Target = '',
    [switch]$Silent
)

# Install the MAI mod into a Mindustry saves\mods folder, then check every copied
# file against the SHA256 list in VERSION.txt.  Nothing is written outside that
# folder (the old copy is moved aside as MlogAuxiliary.bak-<stamp>, not deleted).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$modName = 'MlogAuxiliary'

function Fail([string]$msg) {
    Write-Host ('[fail] ' + $msg) -ForegroundColor Red
    exit 1
}

$verFile = Join-Path $root 'VERSION.txt'
if (-not (Test-Path -LiteralPath $verFile)) {
    Fail 'VERSION.txt is missing -- unpack the whole zip, do not copy single files out of it'
}

$modVer = ''
$sideVer = ''
$hashes = @{}
foreach ($l in @(Get-Content -LiteralPath $verFile -Encoding UTF8)) {
    if ($l -match '^MAI\s+(\S+)') { $modVer = $Matches[1] }
    elseif ($l -match '^sidecar\s+(\S+)') { $sideVer = $Matches[1] }
    elseif ($l -match '^sha256\s+([0-9A-Fa-f]{64})\s+(.+)$') { $hashes[$Matches[2].Trim()] = $Matches[1].ToUpper() }
}

function Find-ModsDir {
    # Which folder Mindustry actually reads is not a guess, it is decided in
    # Vars.loadSettings():
    #     if(steam || Version.isSteam){ settings.setDataDirectory(Core.files.local("saves/")); }
    # (Mindustry-Source/core/src/mindustry/Vars.java:494-495) -- on a Steam build
    # the data directory is saves\ NEXT TO THE GAME, and only a non-Steam build
    # falls back to %APPDATA%\Mindustry.  So a game folder that has a saves\
    # directory wins; APPDATA is the fallback, not the first guess.  Getting this
    # order wrong installs into a folder the game never opens, and the player just
    # sees "the mod does nothing".
    $roots = @()
    if ($env:MAI_STEAM_ROOTS) {
        $roots = $env:MAI_STEAM_ROOTS -split ';'
    } else {
        foreach ($d in @('C', 'D', 'E', 'F', 'G', 'H')) {
            $roots += ($d + ':\Program Files (x86)\Steam\steamapps\common\Mindustry')
            $roots += ($d + ':\Program Files\Steam\steamapps\common\Mindustry')
            $roots += ($d + ':\SteamLibrary\steamapps\common\Mindustry')
            $roots += ($d + ':\Steam\steamapps\common\Mindustry')
        }
    }
    foreach ($r in $roots) {
        if ($r -eq '') { continue }
        $r = $r.TrimEnd('\')
        if (Test-Path -LiteralPath (Join-Path $r 'saves')) { return (Join-Path $r 'saves\mods') }
    }
    $appdata = $env:MAI_APPDATA
    if (-not $appdata) { $appdata = $env:APPDATA }
    if ($appdata) {
        # a candidate counts when ...\Mindustry\saves exists, because saves\mods
        # itself is only created by the game once a mod is installed
        $p = Join-Path $appdata 'Mindustry\saves\mods'
        if (Test-Path -LiteralPath (Split-Path -Parent $p)) { return $p }
    }
    return ''
}

if ($Target -ne '') {
    $mods = $Target
} else {
    $mods = Find-ModsDir
    if ($mods -eq '') {
        if ($Silent) { Fail 'cannot find a Mindustry saves folder -- pass -Target <path to saves\mods>' }
        Write-Host '[warn] cannot find a Mindustry saves folder automatically.' -ForegroundColor Yellow
        $mods = Read-Host 'paste the full path of ...\Mindustry\saves\mods (empty line = cancel)'
        if ($mods -eq '') { Fail 'cancelled' }
    } else {
        Write-Host ('[info] auto-detected saves folder: ' + $mods) -ForegroundColor Cyan
    }
}

if (-not (Test-Path -LiteralPath $mods)) { New-Item -ItemType Directory -Force -Path $mods | Out-Null }

$dest = Join-Path $mods $modName
$bak = ''
if (Test-Path -LiteralPath $dest) {
    $bak = $dest + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Move-Item -LiteralPath $dest -Destination $bak
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Path (Join-Path $root 'mod\MlogAuxiliary\*') -Destination $dest -Recurse -Force

$bad = New-Object System.Collections.ArrayList
foreach ($rel in $hashes.Keys) {
    if ($rel -notlike 'mod/MlogAuxiliary/*') { continue }
    $sub = $rel.Substring('mod/MlogAuxiliary/'.Length).Replace('/', '\')
    $f = Join-Path $dest $sub
    if (-not (Test-Path -LiteralPath $f)) { [void]$bad.Add($sub + ' missing'); continue }
    $h = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToUpper()
    if ($h -ne $hashes[$rel]) { [void]$bad.Add($sub + ' hash mismatch') }
}
if ($bad.Count -gt 0) {
    Fail ('copied, but not byte-identical to the package: ' + ($bad -join '; '))
}

Write-Host ('[ok] MAI ' + $modVer + ' installed to ' + $dest) -ForegroundColor Green
if ($bak -ne '') { Write-Host ('     previous copy moved to ' + $bak) }
Write-Host ('     sidecar ' + $sideVer + ' is bundled in sidecar\bin\logicd.exe -- the mod starts it by itself')
Write-Host ''
Write-Host 'next:'
Write-Host '  1) restart Mindustry (mods load at startup)'
Write-Host '  2) in game: settings -> Mlog辅助 -- fill endpoint / key / model, click 测试连接'
Write-Host '  3) in game: press F3, select processors, describe, confirm'
Write-Host '     (the first F3 also starts the bundled sidecar: "正在启动 sidecar…" -> "sidecar 已就绪")'
