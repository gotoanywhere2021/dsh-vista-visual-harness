# VISTA visual harness — installer for a DeepSeek Harness profile
#
# Run from a clone of this repository:
#   install.cmd            (or: powershell -ExecutionPolicy Bypass -File install.ps1)
#   install.cmd -Uninstall
#
# Steps: back up the profile config, link this package with the pnpm bundled in
# the DSH runtime, select the bundle in dsh.profile.bundles. The running Harness
# hot-recomposes the profile, so the vista_* tools appear on the next request.
# Changed plugin JavaScript still needs a host restart to load.

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$pluginDir = $PSScriptRoot
$package = Get-Content -LiteralPath (Join-Path $pluginDir 'package.json') -Raw | ConvertFrom-Json
$pluginName = $package.name

$profileDir = $env:DSH_PROFILE_DIR
if (-not $profileDir) {
    $profileName = if ($env:DSH_PROFILE) { $env:DSH_PROFILE } else { 'desktop' }
    $profileDir = Join-Path (Join-Path $env:USERPROFILE '.dsh\profiles') $profileName
}
$profileDir = [System.IO.Path]::GetFullPath($profileDir)
if (-not (Test-Path -LiteralPath (Join-Path $profileDir 'package.json'))) {
    throw "not a DSH profile: $profileDir"
}

# ---- locate the DSH bundled node + pnpm -------------------------------------
$node = $env:VISTA_NODE
if (-not $node) {
    $runtimes = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes'
    if (Test-Path -LiteralPath $runtimes) {
        foreach ($runtime in Get-ChildItem -LiteralPath $runtimes -Directory) {
            $candidate = Join-Path $runtime.FullName 'dependencies\node\bin\node.exe'
            if (Test-Path -LiteralPath $candidate) { $node = $candidate; break }
        }
    }
}
if (-not $node) { $node = (Get-Command node -ErrorAction SilentlyContinue).Source }
if (-not $node) { throw 'no node executable found; set $env:VISTA_NODE' }

$pnpm = $env:VISTA_PNPM
if (-not $pnpm) {
    $runtimes = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes'
    if (Test-Path -LiteralPath $runtimes) {
        foreach ($runtime in Get-ChildItem -LiteralPath $runtimes -Directory) {
            $candidate = Join-Path $runtime.FullName 'dependencies\pnpm\bin\pnpm.mjs'
            if (Test-Path -LiteralPath $candidate) { $pnpm = $candidate; break }
        }
    }
}
if (-not $pnpm) { throw 'no pnpm found; set $env:VISTA_PNPM to pnpm.mjs' }

# ---- backup ----------------------------------------------------------------
$backupDir = Join-Path $pluginDir ('.backup\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
foreach ($file in @('package.json', 'cordis.patch.yml', 'pnpm-lock.yaml', 'pnpm-workspace.yaml')) {
    $source = Join-Path $profileDir $file
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $backupDir $file) -Force
    }
}
Write-Host "backup: $backupDir"

# ---- edit the bundle selection ---------------------------------------------
$env:VISTA_PLUGIN_PKG = Join-Path $profileDir 'package.json'
$env:VISTA_PLUGIN_NAME = $pluginName
$env:VISTA_PLUGIN_MODE = if ($Uninstall) { 'remove' } else { 'add' }
$editor = @'
const fs = require('node:fs');
const pkgPath = process.env.VISTA_PLUGIN_PKG;
const name = process.env.VISTA_PLUGIN_NAME;
const mode = process.env.VISTA_PLUGIN_MODE;
const data = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
data.dsh ??= {};
data.dsh.profile ??= {};
data.dsh.profile.bundles ??= [];
const had = data.dsh.profile.bundles.includes(name);
if (mode === 'add' && !had) data.dsh.profile.bundles.push(name);
if (mode === 'remove') data.dsh.profile.bundles = data.dsh.profile.bundles.filter((entry) => entry !== name);
fs.writeFileSync(pkgPath, JSON.stringify(data, null, 2) + '\n');
console.log(`${mode === 'add' ? (had ? 'already selected' : 'selected') : 'unselected'} ${name}; bundles=${data.dsh.profile.bundles.length}`);
'@
& $node -e $editor

# ---- package operations -----------------------------------------------------
if ($Uninstall) {
    & $node $pnpm remove $pluginName --dir $profileDir
} else {
    & $node $pnpm add "link:$($pluginDir -replace '\\','/')" --dir $profileDir
}
if ($LASTEXITCODE -ne 0) { throw "pnpm exited with $LASTEXITCODE" }

Write-Host ''
if ($Uninstall) {
    Write-Host 'VISTA plugin removed from the profile. A host restart unloads the running generation.'
} else {
    Write-Host 'VISTA plugin installed and selected.'
    Write-Host 'The running app hot-recomposes the profile, so the vista_* tools appear on the next model request.'
    Write-Host 'Verify with: vista_status  (or inspect the Plugins page in the sidebar).'
    Write-Host "Runs live under: $(Join-Path $env:USERPROFILE '.dsh\vista\runs')"
}
