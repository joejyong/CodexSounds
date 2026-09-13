[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('codex-sounds-build-' + [Guid]::NewGuid().ToString('N'))
$distribution = Join-Path $temporaryRoot 'dist'
$work = Join-Path $temporaryRoot 'work'
$spec = Join-Path $temporaryRoot 'spec'
$built = Join-Path $distribution 'codex-sounds'
$destination = Join-Path $pluginRoot 'bin\codex-sounds'
$settingsPage = Join-Path $pluginRoot 'src\settings.html'

try {
    New-Item -ItemType Directory -Path $distribution,$work,$spec -Force | Out-Null
    Push-Location $pluginRoot
    try {
        python -m PyInstaller --noconfirm --clean --onedir --noconsole --name codex-sounds `
            --add-data ($settingsPage + ';.') --distpath $distribution --workpath $work --specpath $spec `
            'src\app.py'
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath (Join-Path $built 'codex-sounds.exe'))) {
        throw 'The packaged executable is missing.'
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath($pluginRoot)
    $resolvedDestination = [System.IO.Path]::GetFullPath($destination)
    if (-not $resolvedDestination.StartsWith($resolvedRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to replace a binary directory outside the plugin.'
    }
    if (Test-Path -LiteralPath $destination) {
        Remove-Item -LiteralPath $destination -Recurse -Force
    }
    Move-Item -LiteralPath $built -Destination $destination
    Write-Output $destination
} finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
