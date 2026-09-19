param(
    [string]$DDPackagePath,
    [switch]$ConfirmDDRedistributionRights
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7 is required.' }
$root = $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
$node = (Get-Command node -CommandType Application | Select-Object -First 1).Source
$npm = Join-Path (Split-Path $node -Parent) 'node_modules/npm/bin/npm-cli.js'
$cargo = Join-Path $HOME '.cargo/bin/cargo.exe'
if (-not (Test-Path -LiteralPath $cargo)) { $cargo = (Get-Command cargo -CommandType Application).Source }
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync first.' }
if ($DDPackagePath -and -not $ConfirmDDRedistributionRights) {
    throw 'Bundling DD requires explicit confirmation of third-party redistribution rights.'
}
$stage = Join-Path $root ('.build/bundle-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$oldPath = $env:PATH
$oldShell = $env:npm_config_script_shell
$oldTarget = $env:CARGO_TARGET_DIR
$target = Join-Path $root '.build/installer-target'
try {
    $env:CARGO_TARGET_DIR = $target
    $env:PATH = "$(Split-Path $cargo -Parent);$oldPath"
    $env:npm_config_script_shell = (Get-Process -Id $PID).Path
    & $python -m PyInstaller --noconfirm --distpath "$stage/runtime" --workpath "$stage/work" "$root/packaging/engine.spec"
    if ($LASTEXITCODE -ne 0) { throw 'Engine bundling failed.' }
    & "$root/packaging/Test-Engine.ps1" -EngineDirectory "$stage/runtime/rock-music-engine"
    $resources = @{ (Join-Path $stage 'runtime/rock-music-engine/') = 'engine/' }
    $resources[(Join-Path $root 'LICENSE')] = 'licenses/LICENSE'
    $resources[(Join-Path $root 'THIRD_PARTY_NOTICES.md')] = 'licenses/THIRD_PARTY_NOTICES.md'
    if ($DDPackagePath) {
        $dd = (Resolve-Path -LiteralPath $DDPackagePath).Path
        $ddStage = Join-Path $stage 'drivers/dd'
        New-Item -ItemType Directory -Path "$ddStage/drv" -Force | Out-Null
        $files = @('ddhid.63340.dll', 'drv/ddc.exe', 'drv/ddhid63340.inf', 'drv/ddhid63340.sys', 'drv/ddhid63340.cat')
        foreach ($file in $files) {
            $source = Join-Path $dd $file
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing DD package file: $file" }
            if ($file -in @('ddhid.63340.dll', 'drv/ddc.exe', 'drv/ddhid63340.cat')) {
                if ((Get-AuthenticodeSignature -LiteralPath $source).Status -ne 'Valid') {
                    throw "DD signature validation failed: $file"
                }
            }
            Copy-Item -LiteralPath $source -Destination (Join-Path $ddStage $file)
        }
        $resources[$ddStage + '/'] = 'drivers/dd/'
    }
    $config = @{
        bundle = @{
            active = $true
            targets = @('nsis')
            resources = $resources
            windows = @{
                nsis = @{
                    installMode = 'perMachine'
                    installerHooks = (Join-Path $root 'packaging/installer-hooks.nsh')
                    languages = @('SimpChinese', 'English')
                    displayLanguageSelector = $true
                }
            }
        }
    }
    $configPath = Join-Path $stage 'bundle.json'
    $config | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $configPath -Encoding UTF8
    Push-Location "$root/desktop"
    try {
        & $node $npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        & $node $npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
        & $node node_modules/@tauri-apps/cli/tauri.js build --config $configPath
        if ($LASTEXITCODE -ne 0) { throw 'Tauri installer build failed.' }
    }
    finally { Pop-Location }
}
finally {
    $env:PATH = $oldPath
    $env:npm_config_script_shell = $oldShell
    $env:CARGO_TARGET_DIR = $oldTarget
}
Write-Host "Installer: $target/release/bundle/nsis"
Write-Host 'No driver was installed on this build machine. Test the installer in a clean Windows VM.'
