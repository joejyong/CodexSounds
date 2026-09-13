[CmdletBinding()]
param(
    [ValidateSet('Setup','Settings','Status','Mute','Unmute','AmbientOn','AmbientOff','AmbientMute','AmbientUnmute','AmbientToggle','Disconnect')]
    [string]$Action = 'Settings'
)
$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$executable = Join-Path $pluginRoot 'bin\codex-sounds\codex-sounds.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'The bundled Windows player is missing. Reinstall Codex Sounds.' }
$codexDirectory = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$dataDirectory = Join-Path $codexDirectory 'notification-sounds'
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
$report = Join-Path $dataDirectory ('command-' + [Guid]::NewGuid().ToString('N') + '.json')
try {
    $command = switch ($Action) {
        'AmbientOn' { 'ambient-on' }
        'AmbientOff' { 'ambient-off' }
        'AmbientMute' { 'ambient-mute' }
        'AmbientUnmute' { 'ambient-unmute' }
        'AmbientToggle' { 'ambient-toggle' }
        default { $Action.ToLowerInvariant() }
    }
    $arguments = @('--codex-home', ('"' + $codexDirectory + '"'), '--report', ('"' + $report + '"'), $command)
    $process = Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden -PassThru
    # Wait for this command only. Settings starts a server that stays running.
    $process.WaitForExit()
    if (-not (Test-Path -LiteralPath $report)) { throw 'The sound helper did not produce a result.' }
    $result = Get-Content -LiteralPath $report -Raw
    if ($process.ExitCode -ne 0) { throw $result }
    $result
} finally {
    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report }
}
