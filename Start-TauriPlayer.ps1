$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Please run this script with PowerShell 7 (pwsh).' }

$exePath = Join-Path $PSScriptRoot 'desktop\src-tauri\target\release\rock-music-player.exe'
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw 'Release executable not found. Run ./Build-TauriPlayer.ps1 with PowerShell 7 first.'
}
$oldHome = $env:ROCK_MUSIC_HOME
try {
    if (-not $env:ROCK_MUSIC_HOME) { $env:ROCK_MUSIC_HOME = $PSScriptRoot }
    Start-Process -FilePath $exePath -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
finally { $env:ROCK_MUSIC_HOME = $oldHome }
