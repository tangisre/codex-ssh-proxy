param(
    [ValidateSet('Desktop', 'Verify', 'Login', 'Run', 'Smoke', 'Probe')]
    [string]$Mode = 'Run',

    [string]$HostAlias = 'codex-linux',
    [int]$WindowsProxyPort = 7890,
    [string]$Project = '~',
    [string]$Python = 'python.exe'
)

$ErrorActionPreference = 'Stop'
$entryPoint = Join-Path $PSScriptRoot 'start.py'
& $Python $entryPoint ($Mode.ToLowerInvariant()) --host $HostAlias --windows-proxy-port $WindowsProxyPort --project $Project
exit $LASTEXITCODE
