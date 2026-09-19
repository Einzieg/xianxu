$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Please run this script with PowerShell 7 (pwsh).' }
& (Join-Path $PSScriptRoot 'Start-TauriPlayer.ps1')
