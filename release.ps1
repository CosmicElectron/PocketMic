<#
.SYNOPSIS
    Builds, tests and publishes a PocketMic release on GitHub, where Obtainium picks it up.

.DESCRIPTION
    Sets the version in VERSION and pc/pocketmic/__init__.py, runs both test suites, builds the
    signed release APK and the Windows receiver, then commits, tags, pushes and creates the
    GitHub release with both files attached.

.EXAMPLE
    .\release.ps1 1.0.1
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$tag = "v$Version"
# SHA-256 of the release certificate. An APK signed with any other key cannot update installed copies.
$releaseCert = 'f80dd6c0af32118741d30dfc47fc8863ab68650b21eedb0f470a46e3af7c7bf3'

function Invoke-Checked([string]$What, [scriptblock]$Command) {
    Write-Host "==> $What" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

Push-Location $PSScriptRoot
try {
    $keystoreProperties = $env:POCKETMIC_KEYSTORE_PROPERTIES
    if (-not $keystoreProperties) { $keystoreProperties = Join-Path $HOME '.pocketmic\keystore.properties' }
    if (-not (Test-Path $keystoreProperties)) {
        throw "Release signing key not found at $keystoreProperties. Restore your backup of the .pocketmic folder; a new key would stop Obtainium from updating the installed app."
    }
    if (git status --porcelain) { throw 'Commit or stash your changes first: releases are built from a clean tree.' }
    if (git tag --list $tag) { throw "Tag $tag already exists." }
    gh auth status | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not logged in. Run: gh auth login' }
    if (-not $env:JAVA_HOME) { $env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr' }

    Write-Host "==> Setting version $Version" -ForegroundColor Cyan
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $PWD 'VERSION'), "$Version`n", $utf8)
    $init = Join-Path $PWD 'pc\pocketmic\__init__.py'
    $text = [IO.File]::ReadAllText($init) -replace '__version__ = "[^"]*"', "__version__ = `"$Version`""
    [IO.File]::WriteAllText($init, $text, $utf8)

    Push-Location pc
    try { Invoke-Checked 'PC receiver tests' { python -m pytest -q -p no:cacheprovider tests } }
    finally { Pop-Location }

    Push-Location android
    try { Invoke-Checked 'Android tests, lint and release build' { .\gradlew.bat --console=plain testDebugUnitTest lintDebug assembleRelease } }
    finally { Pop-Location }

    $apk = Join-Path $PWD 'android\app\build\outputs\apk\release\app-release.apk'
    if (-not (Test-Path $apk)) { throw 'No signed APK was built. Check the signing key settings in keystore.properties.' }
    $sdk = $env:ANDROID_HOME
    if (-not $sdk) { $sdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
    $apksigner = Get-ChildItem (Join-Path $sdk 'build-tools') -Directory | Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'apksigner.bat' } | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $apksigner) { throw "apksigner not found under $sdk\build-tools." }
    $certs = & $apksigner verify --print-certs $apk
    if ($LASTEXITCODE -ne 0) { throw 'apksigner could not verify the APK signature.' }
    if (-not ($certs -match "SHA-256 digest: $releaseCert")) { throw "The APK is not signed with the PocketMic release key:`n$certs" }

    Invoke-Checked 'Windows receiver build' { & .\pc\build_exe.bat }

    $out = Join-Path $PWD 'release'
    New-Item -ItemType Directory -Force $out | Out-Null
    $apkOut = Join-Path $out "PocketMic-$tag.apk"
    $zipOut = Join-Path $out "PocketMicReceiver-$tag-windows.zip"
    Copy-Item $apk $apkOut -Force
    if (Test-Path $zipOut) { Remove-Item $zipOut }
    Write-Host '==> Zipping the Windows receiver' -ForegroundColor Cyan
    Compress-Archive -Path (Join-Path $PWD 'pc\dist\PocketMicReceiver') -DestinationPath $zipOut

    git add VERSION pc/pocketmic/__init__.py
    if (git status --porcelain) { Invoke-Checked 'Commit version bump' { git commit -q -m "Release $tag" } }
    Invoke-Checked 'Tag' { git tag -a $tag -m "PocketMic $tag" }
    Invoke-Checked 'Push' { git push -q origin HEAD $tag }

    $notes = @"
**Phone:** install ``PocketMic-$tag.apk``, or add this repository in [Obtainium](https://github.com/ImranR98/Obtainium) to get updates automatically.
**PC:** unzip ``PocketMicReceiver-$tag-windows.zip`` and run ``PocketMicReceiver.exe``. Needs [VB-Audio Virtual Cable](https://vb-audio.com/Cable/).
"@
    Invoke-Checked 'GitHub release' { gh release create $tag $apkOut $zipOut --title "PocketMic $tag" --notes $notes --generate-notes }
    Write-Host "Released $tag" -ForegroundColor Green
}
finally {
    Pop-Location
}
