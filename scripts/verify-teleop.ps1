[CmdletBinding()]
param(
    [switch]$SkipSdkCheck,
    [switch]$SkipAndroid,
    [switch]$SkipHarmonyBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host "`n==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Resolve-Executable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Required executable is not available: $Name"
    }
    return $command.Source
}

function Resolve-AndroidJavaHome {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:JAVA_HOME)) {
        $candidates += $env:JAVA_HOME
    }

    $portableRoot = Join-Path $repoRoot 'tmp\android-tools\jdk-extract'
    if (Test-Path -LiteralPath $portableRoot) {
        $candidates += Get-ChildItem -LiteralPath $portableRoot -Directory |
            Select-Object -ExpandProperty FullName
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'bin\jlink.exe')) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw 'Android build needs a complete JDK 17+ containing bin\jlink.exe. Set JAVA_HOME or place it under tmp\android-tools\jdk-extract.'
}

function Resolve-AndroidSdkRoot {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:ANDROID_SDK_ROOT)) {
        $candidates += $env:ANDROID_SDK_ROOT
    }
    $candidates += Join-Path $repoRoot 'tmp\android-sdk'

    foreach ($candidate in $candidates) {
        $platform = Join-Path $candidate 'platforms\android-35\android.jar'
        $buildTools = Join-Path $candidate 'build-tools\35.0.0'
        if ((Test-Path -LiteralPath $platform) -and (Test-Path -LiteralPath $buildTools)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw 'Android SDK Platform 35 and Build Tools 35.0.0 were not found. Set ANDROID_SDK_ROOT or provision tmp\android-sdk.'
}

function Resolve-DevEcoRoot {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:DEVECO_HOME)) {
        $candidates += $env:DEVECO_HOME
    }
    $candidates += 'C:\Program Files\Huawei\DevEco Studio'

    foreach ($candidate in $candidates) {
        $hvigor = Join-Path $candidate 'tools\hvigor\bin\hvigorw.bat'
        $sdk = Join-Path $candidate 'sdk'
        $node = Join-Path $candidate 'tools\node'
        $java = Join-Path $candidate 'jbr\bin\java.exe'
        if ((Test-Path -LiteralPath $hvigor) -and
            (Test-Path -LiteralPath $sdk) -and
            (Test-Path -LiteralPath $node) -and
            (Test-Path -LiteralPath $java)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw 'DevEco Studio with Hvigor, Node, JBR and HarmonyOS SDK was not found. Set DEVECO_HOME or install it in the default location.'
}

Push-Location $repoRoot
try {
    if (-not $SkipSdkCheck) {
        Invoke-Checked 'Verify pinned SDK submodules' {
            & (Join-Path $repoRoot 'scripts\manage-sdks.ps1') -VerifyOnly
        }
    }

    $python = Resolve-Executable 'python'
    Invoke-Checked 'Compile Python bridge' {
        & $python -m compileall -q teleop\bridge\src teleop\bridge\tests
    }
    Invoke-Checked 'Run Python bridge tests' {
        & $python -m pytest teleop\bridge\tests -q
    }

    if (-not $SkipAndroid) {
        $androidJavaHome = Resolve-AndroidJavaHome
        $androidSdkRoot = Resolve-AndroidSdkRoot
        $oldJavaHome = $env:JAVA_HOME
        $oldAndroidSdkRoot = $env:ANDROID_SDK_ROOT
        try {
            $env:JAVA_HOME = $androidJavaHome
            $env:ANDROID_SDK_ROOT = $androidSdkRoot
            Push-Location (Join-Path $repoRoot 'teleop\android')
            try {
                Invoke-Checked 'Run Android JVM tests' {
                    & .\gradlew.bat --no-daemon testDebugUnitTest
                }
                Invoke-Checked 'Build Android debug APK' {
                    & .\gradlew.bat --no-daemon assembleDebug
                }
                Invoke-Checked 'Run Android lint' {
                    & .\gradlew.bat --no-daemon lintDebug
                }
            }
            finally {
                Pop-Location
            }
        }
        finally {
            $env:JAVA_HOME = $oldJavaHome
            $env:ANDROID_SDK_ROOT = $oldAndroidSdkRoot
        }
    }

    Invoke-Checked 'Run HarmonyOS static protocol checks' {
        & (Join-Path $repoRoot 'teleop\harmony\scripts\verify-static.ps1')
    }

    if (-not $SkipHarmonyBuild) {
        $devEcoRoot = Resolve-DevEcoRoot
        $hvigor = Join-Path $devEcoRoot 'tools\hvigor\bin\hvigorw.bat'
        $oldJavaHome = $env:JAVA_HOME
        $oldNodeHome = $env:NODE_HOME
        $oldDevEcoSdkHome = $env:DEVECO_SDK_HOME
        $oldPath = $env:Path
        try {
            $env:JAVA_HOME = Join-Path $devEcoRoot 'jbr'
            $env:NODE_HOME = Join-Path $devEcoRoot 'tools\node'
            $env:DEVECO_SDK_HOME = Join-Path $devEcoRoot 'sdk'
            $env:Path = "$env:JAVA_HOME\bin;$env:NODE_HOME;$($devEcoRoot)\tools\ohpm\bin;$oldPath"
            Push-Location (Join-Path $repoRoot 'teleop\harmony')
            try {
                Invoke-Checked 'Build HarmonyOS unsigned HAP with ArkTS type checking' {
                    & $hvigor --no-daemon --no-incremental --type-check assembleHap
                }
            }
            finally {
                Pop-Location
            }
        }
        finally {
            $env:JAVA_HOME = $oldJavaHome
            $env:NODE_HOME = $oldNodeHome
            $env:DEVECO_SDK_HOME = $oldDevEcoSdkHome
            $env:Path = $oldPath
        }
    }

    Invoke-Checked 'Check unstaged patch whitespace' {
        & git diff --check
    }
    Invoke-Checked 'Check staged patch whitespace' {
        & git diff --cached --check
    }

    Write-Host "`nAll requested LM3-UP teleoperation checks passed."
}
finally {
    Pop-Location
}
