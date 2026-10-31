param(
    [string]$Target = '',
    [switch]$Silent
)

# Remove the mod folder from a Mindustry saves\mods directory.  Backups left by
# install.ps1 (MlogAuxiliary.bak-*) are not touched.

$ErrorActionPreference = 'Stop'
$modName = 'MlogAuxiliary'

function Find-ModsDir {
    # Same order as install.ps1, for the same reason: on a Steam build the data
    # directory is saves\ next to the game (Vars.java:494-495), %APPDATA% only on
    # a non-Steam build.  Removing from the wrong folder leaves the real mod in
    # place while reporting success.
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
        if ($Silent) { Write-Host '[fail] cannot find a Mindustry saves folder'; exit 1 }
        $mods = Read-Host 'paste the full path of ...\Mindustry\saves\mods (empty line = cancel)'
        if ($mods -eq '') { Write-Host '[fail] cancelled'; exit 1 }
    }
}

$dest = Join-Path $mods $modName
if (Test-Path -LiteralPath $dest) {
    Remove-Item -LiteralPath $dest -Recurse -Force
    Write-Host ('[ok] removed ' + $dest) -ForegroundColor Green
} else {
    Write-Host ('[ok] nothing to remove at ' + $dest)
}
Write-Host '     restart Mindustry to unload the mod'
