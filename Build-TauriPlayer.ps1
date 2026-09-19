$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Please run this script with PowerShell 7 (pwsh).' }

$pwshPath = (Get-Process -Id $PID).Path
$nodePath = (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$npmCli = Join-Path (Split-Path -Parent $nodePath) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $npmCli -PathType Leaf)) { throw "npm-cli.js not found: $npmCli" }
$cargoCommand = Get-Command cargo -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$cargoPath = if ($cargoCommand) { $cargoCommand.Source } else { Join-Path $HOME '.cargo\bin\cargo.exe' }
if (-not (Test-Path -LiteralPath $cargoPath -PathType Leaf)) {
    throw 'Cargo is missing. Install official Rust stable (MSVC) with rustup --profile minimal --no-modify-path.'
}

$oldPath = $env:PATH
$oldScriptShell = $env:npm_config_script_shell
$oldTargetDir = $env:CARGO_TARGET_DIR
try {
    $env:PATH = "$(Split-Path -Parent $cargoPath);$oldPath"
    $env:npm_config_script_shell = $pwshPath
    $env:CARGO_TARGET_DIR = Join-Path $PSScriptRoot 'desktop\src-tauri\target'
    Push-Location -LiteralPath (Join-Path $PSScriptRoot 'desktop')
    try {
        if (-not (Test-Path -LiteralPath 'package.json')) { throw 'Frontend package.json is not available yet.' }
        if (-not (Test-Path -LiteralPath 'node_modules')) {
            $installCommand = if (Test-Path -LiteralPath 'package-lock.json') { 'ci' } else { 'install' }
            & $nodePath $npmCli $installCommand
            if ($LASTEXITCODE -ne 0) { throw "npm $installCommand failed with exit code $LASTEXITCODE" }
        }
        & $nodePath $npmCli run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed with exit code $LASTEXITCODE" }
        & $cargoPath check --locked --manifest-path 'src-tauri\Cargo.toml'
        if ($LASTEXITCODE -ne 0) { throw "cargo check failed with exit code $LASTEXITCODE" }
        & $cargoPath build --locked --release --features custom-protocol --manifest-path 'src-tauri\Cargo.toml'
        if ($LASTEXITCODE -ne 0) { throw "cargo build failed with exit code $LASTEXITCODE" }
    }
    finally { Pop-Location }
}
finally {
    $env:PATH = $oldPath
    $env:npm_config_script_shell = $oldScriptShell
    $env:CARGO_TARGET_DIR = $oldTargetDir
}
Write-Host "Built: $(Join-Path $PSScriptRoot 'desktop\src-tauri\target\release\rock-music-player.exe')"
Write-Host 'Workspace-dependent release only; Python and the virtual environment are not bundled.'
