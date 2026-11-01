[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$Output,
    [ValidateSet('Preflight', 'HostCheck', 'Execute')][string]$Mode = 'Preflight',
    [string]$Python = 'python',
    [string]$Context = 'desktop-linux',
    [string]$ExpectedEngineId
)

$ErrorActionPreference = 'Stop'
$configPath = [System.IO.Path]::GetFullPath($Config)
$outputPath = [System.IO.Path]::GetFullPath($Output)
$repoPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$originalAppdata = [Environment]::GetEnvironmentVariable('WIN_PD_OVERRIDE_LOCAL_APPDATA', 'Process')
$originalRoaming = [Environment]::GetEnvironmentVariable('WIN_PD_OVERRIDE_ROAMING_APPDATA', 'Process')
$exitCode = 2
Push-Location $repoPath
try {
    # Preflight does not touch Docker, alter context/config, or create output.
    & $Python -m agent_runtime.swe_prepare $configPath $outputPath
    if ($LASTEXITCODE -ne 0) { throw 'Offline input preflight failed; no container was created.' }
    if ($Mode -eq 'Preflight') { $exitCode = 0; return }
    if ($Mode -eq 'Execute' -and -not $ExpectedEngineId) {
        throw 'Execute requires -ExpectedEngineId copied from a trusted HostCheck.'
    }
    # Docker SDK does not automatically follow Docker CLI contexts.
    $endpoint = & docker context inspect $Context --format '{{.Endpoints.docker.Host}}'
    if ($LASTEXITCODE -ne 0 -or -not $endpoint) { throw 'Cannot resolve the selected Docker context.' }
    $endpoint = ($endpoint | Out-String).Trim()
    if ($endpoint -notmatch '^(npipe|unix)://') {
        throw 'This local entry point supports npipe/unix endpoints only; configure remote TLS separately.'
    }
    $hostArgs = @('-m', 'agent_runtime.swe_prepare', $configPath, $outputPath, '--host', '--docker-host', $endpoint)
    if ($ExpectedEngineId) { $hostArgs += @('--expected-engine-id', $ExpectedEngineId) }
    & $Python @hostArgs
    if ($LASTEXITCODE -ne 0) { throw 'Host/image check failed; no container was created.' }
    if ($Mode -eq 'HostCheck') { $exitCode = 0; return }

    $reportInputs = $outputPath + '-report-inputs'
    $reportOutput = $outputPath + '-report'
    $localAppdata = $outputPath + '-inspect-appdata'
    $roamingAppdata = $outputPath + '-inspect-roaming'
    if ((Test-Path -LiteralPath $reportInputs) -or (Test-Path -LiteralPath $reportOutput) -or
        (Test-Path -LiteralPath $localAppdata) -or (Test-Path -LiteralPath $roamingAppdata)) {
        throw 'Report directories already exist; choose a fresh output. No automatic retry.'
    }
    # Only process-local appdata values; restore them even after a failed run.
    [Environment]::SetEnvironmentVariable('WIN_PD_OVERRIDE_LOCAL_APPDATA', $localAppdata, 'Process')
    [Environment]::SetEnvironmentVariable('WIN_PD_OVERRIDE_ROAMING_APPDATA', $roamingAppdata, 'Process')
    & $Python -m agent_runtime.swe_acceptance $configPath $outputPath --execute --docker-host $endpoint --expected-engine-id $ExpectedEngineId
    $exitCode = $LASTEXITCODE
    $resultPath = Join-Path $outputPath 'result.json'
    if (Test-Path -LiteralPath $resultPath) {
        # Collect diagnostics even on BLOCKED; never execute the candidate again.
        New-Item -ItemType Directory -Path $reportInputs | Out-Null
        $digest = (Get-FileHash -LiteralPath $resultPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifest = @{
            schema_version = 'evaluation-report-input-v1'
            entries = @(@{ id = 'acceptance'; kind = 'acceptance'; scope = 'real_container'; path = $resultPath; sha256 = $digest })
        } | ConvertTo-Json -Depth 6
        $manifestPath = Join-Path $reportInputs 'manifest.json'
        [System.IO.File]::WriteAllText($manifestPath, $manifest, [System.Text.UTF8Encoding]::new($false))
        & $Python -m agent_runtime.evaluation_report $manifestPath $reportOutput
        if ($LASTEXITCODE -ne 0) { $exitCode = 2 }
    }
}
catch {
    $exitCode = 2
    Write-Error $_ -ErrorAction Continue
}
finally {
    [Environment]::SetEnvironmentVariable('WIN_PD_OVERRIDE_LOCAL_APPDATA', $originalAppdata, 'Process')
    [Environment]::SetEnvironmentVariable('WIN_PD_OVERRIDE_ROAMING_APPDATA', $originalRoaming, 'Process')
    Pop-Location
    exit $exitCode
}
