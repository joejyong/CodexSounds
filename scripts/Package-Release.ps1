[CmdletBinding()]
param(
    [string]$Version,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $pluginRoot '.codex-plugin\plugin.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$manifestVersion = [string]$manifest.version
$baseVersion = $manifestVersion.Split('+')[0]
if (-not $Version) { $Version = $baseVersion }
if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
    throw "Invalid release version: $Version"
}
if ($Version -ne $baseVersion) {
    throw "Release version $Version does not match manifest version $baseVersion."
}

if (-not $OutputDirectory) { $OutputDirectory = Join-Path $pluginRoot 'outputs' }
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('codex-sounds-release-' + [Guid]::NewGuid().ToString('N'))
$stagingRoot = Join-Path $temporaryRoot 'codex-sounds'
$archive = Join-Path $outputRoot 'codex-sounds-windows-x64.zip'
$checksum = $archive + '.sha256'
$requiredPaths = @(
    '.codex-plugin', '.mcp.json', 'README.md', 'bin', 'licenses', 'mcp-server.mjs',
    'node_modules', 'package.json', 'package-lock.json', 'requirements-build.txt',
    'scripts', 'skills', 'src', 'tests'
)

try {
    New-Item -ItemType Directory -Path $stagingRoot,$outputRoot -Force | Out-Null
    foreach ($relativePath in $requiredPaths) {
        $source = Join-Path $pluginRoot $relativePath
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Release input is missing: $relativePath"
        }
        Copy-Item -LiteralPath $source -Destination $stagingRoot -Recurse -Force
    }

    $releaseManifestPath = Join-Path $stagingRoot '.codex-plugin\plugin.json'
    $releaseManifest = Get-Content -LiteralPath $releaseManifestPath -Raw | ConvertFrom-Json
    $releaseManifest.version = $Version
    $releaseManifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $releaseManifestPath -Encoding utf8

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Remove-Item -LiteralPath $archive,$checksum -Force -ErrorAction SilentlyContinue
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $temporaryRoot,
        $archive,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $entries = @($zip.Entries | ForEach-Object FullName)
        $requiredEntries = @(
            'codex-sounds/.codex-plugin/plugin.json',
            'codex-sounds/bin/codex-sounds/codex-sounds.exe',
            'codex-sounds/bin/codex-sounds/_internal/_tcl_data/init.tcl',
            'codex-sounds/bin/codex-sounds/_internal/_tk_data/tk.tcl',
            'codex-sounds/bin/codex-sounds/_internal/tcl8/8.6/http-2.9.8.tm',
            'codex-sounds/mcp-server.mjs',
            'codex-sounds/node_modules/@modelcontextprotocol/sdk/package.json',
            'codex-sounds/skills/sound-settings/SKILL.md'
        )
        foreach ($entry in $requiredEntries) {
            if ($entries -notcontains $entry) { throw "Release archive is missing: $entry" }
        }
        $tclDataCount = @($entries | Where-Object { $_ -like 'codex-sounds/bin/codex-sounds/_internal/_tcl_data/*' -and -not $_.EndsWith('/') }).Count
        $tkDataCount = @($entries | Where-Object { $_ -like 'codex-sounds/bin/codex-sounds/_internal/_tk_data/*' -and -not $_.EndsWith('/') }).Count
        $tclModuleCount = @($entries | Where-Object { $_ -like 'codex-sounds/bin/codex-sounds/_internal/tcl8/*' -and -not $_.EndsWith('/') }).Count
        if ($tclDataCount -lt 800) { throw "Release archive contains only $tclDataCount Tcl runtime files." }
        if ($tkDataCount -lt 80) { throw "Release archive contains only $tkDataCount Tk runtime files." }
        if ($tclModuleCount -lt 5) { throw "Release archive contains only $tclModuleCount Tcl module files." }
        if ($entries | Where-Object { $_ -match '(?:^|/)(?:sounds|ambient|web-session|last-event)\.json$|rotation\.sqlite$' }) {
            throw 'Release archive contains workstation settings.'
        }
    } finally {
        $zip.Dispose()
    }

    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $checksum -Value "$hash  codex-sounds-windows-x64.zip" -Encoding ascii
    Write-Output $archive
    Write-Output $checksum
} finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
