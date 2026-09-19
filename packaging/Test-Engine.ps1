param([Parameter(Mandatory)][string]$EngineDirectory)
$ErrorActionPreference = 'Stop'
$runtime = (Resolve-Path -LiteralPath $EngineDirectory).Path
$data = Join-Path ([System.IO.Path]::GetTempPath()) ('xianxu-smoke-' + [guid]::NewGuid().ToString('N'))
$psi = [System.Diagnostics.ProcessStartInfo]::new((Join-Path $runtime 'rock-music-engine.exe'))
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.WorkingDirectory = $runtime
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.ArgumentList.Add('--no-hotkeys')
$psi.ArgumentList.Add('--data-dir')
$psi.ArgumentList.Add($data)
$process = [System.Diagnostics.Process]::Start($psi)
try {
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.WriteLine('{"method":"get_state"}')
    $process.StandardInput.WriteLine('{"method":"shutdown"}')
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(30000)) { throw 'Frozen engine timed out; no playback was requested.' }
    $replies = @($stdout.Result -split '\r?\n' | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
    if ($process.ExitCode -ne 0 -or $replies.Count -ne 2 -or $replies.ok -contains $false) {
        throw "Frozen engine smoke failed: $($stderr.Result) $($stdout.Result)"
    }
    if ($replies[1].result.closed -ne $true) { throw 'Engine did not confirm shutdown.' }
    Write-Host "Frozen engine: get_state + shutdown OK (no hotkeys or playback). Temporary data: $data"
}
finally { $process.Dispose() }
