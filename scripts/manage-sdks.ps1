[CmdletBinding()]
param(
    [switch]$UpdateRemote,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$gitModules = Join-Path $repoRoot '.gitmodules'

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [string]$WorkingDirectory = $repoRoot
    )

    $output = & git -C $WorkingDirectory @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git -C '$WorkingDirectory' $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output
}

if (-not (Test-Path -LiteralPath $gitModules)) {
    throw "Missing .gitmodules under $repoRoot"
}

if (-not $VerifyOnly) {
    Invoke-Git -Arguments @('submodule', 'sync') | Out-Host

    $updateArgs = @('submodule', 'update', '--init')
    if ($UpdateRemote) {
        $updateArgs += '--remote'
    }
    Invoke-Git -Arguments $updateArgs | Out-Host

    # lebai-sdk.rs has one legitimate nested protocol submodule. Initialize it
    # explicitly instead of recursing through every vendor repository.
    $rustSdk = Join-Path $repoRoot 'vendor\lebai\sdk\lebai-sdk.rs'
    Invoke-Git -WorkingDirectory $rustSdk -Arguments @('submodule', 'sync', '--recursive') | Out-Host
    Invoke-Git -WorkingDirectory $rustSdk -Arguments @('submodule', 'update', '--init', '--recursive') | Out-Host
}

$topLevelStatus = @(Invoke-Git -Arguments @('submodule', 'status'))
$badTopLevel = @($topLevelStatus | Where-Object { $_ -match '^[+\-U]' })
if ($badTopLevel.Count -gt 0) {
    throw "Top-level submodule state is not pinned/initialized:`n$($badTopLevel -join "`n")"
}

$records = @(Invoke-Git -Arguments @('config', '-f', '.gitmodules', '--get-regexp', '^submodule\..*\.path$'))
$verified = 0
foreach ($record in $records) {
    $parts = $record -split '\s+', 2
    $key = $parts[0]
    $relativePath = $parts[1]
    $name = $key.Substring(10, $key.Length - 15)
    $fullPath = Join-Path $repoRoot $relativePath

    if (-not (Test-Path -LiteralPath $fullPath)) {
        throw "Missing submodule path: $relativePath"
    }

    $expectedUrl = (Invoke-Git -Arguments @('config', '-f', '.gitmodules', '--get', "submodule.$name.url")) -join ''
    $actualUrl = (Invoke-Git -WorkingDirectory $fullPath -Arguments @('remote', 'get-url', 'origin')) -join ''
    if ($actualUrl.Trim() -ne $expectedUrl.Trim()) {
        throw "Remote mismatch for $relativePath. Expected '$expectedUrl', got '$actualUrl'."
    }

    $trackedFiles = @(Invoke-Git -WorkingDirectory $fullPath -Arguments @('ls-files'))
    if ($trackedFiles.Count -eq 0) {
        throw "Submodule has no tracked files: $relativePath"
    }

    $dirty = @(Invoke-Git -WorkingDirectory $fullPath -Arguments @('status', '--porcelain'))
    if ($dirty.Count -gt 0) {
        throw "Submodule is dirty: $relativePath"
    }

    $commit = ((Invoke-Git -WorkingDirectory $fullPath -Arguments @('rev-parse', '--short=12', 'HEAD')) -join '').Trim()
    Write-Host "OK  $relativePath  $commit  files=$($trackedFiles.Count)"
    $verified++
}

$rustSdk = Join-Path $repoRoot 'vendor\lebai\sdk\lebai-sdk.rs'
$nestedStatus = @(Invoke-Git -WorkingDirectory $rustSdk -Arguments @('submodule', 'status', '--recursive'))
$badNested = @($nestedStatus | Where-Object { $_ -match '^[+\-U]' })
if ($badNested.Count -gt 0) {
    throw "lebai-sdk.rs nested submodule is not pinned/initialized:`n$($badNested -join "`n")"
}

$rosSdk = Join-Path $repoRoot 'vendor\lebai\ros\lebai-ros-sdk'
$isShallow = ((Invoke-Git -WorkingDirectory $rosSdk -Arguments @('rev-parse', '--is-shallow-repository')) -join '').Trim()
if ($isShallow -ne 'false') {
    throw 'lebai-ros-sdk must be a full clone so all ROS distribution branches remain available.'
}

$requiredRosBranches = @('humble-dev', 'jazzy-dev', 'lyrical-dev', 'noetic-dev', 'galactic-dev', 'melodic-dev')
$availableRosBranches = @(Invoke-Git -WorkingDirectory $rosSdk -Arguments @('for-each-ref', '--format=%(refname:strip=3)', 'refs/remotes/origin'))
foreach ($branch in $requiredRosBranches) {
    if ($branch -notin $availableRosBranches) {
        throw "Missing ROS remote branch: $branch"
    }
}

Write-Host "Verified $verified top-level SDK/integration submodules."
Write-Host 'Note: yunji/cloud/open-api contains an upstream malformed docs/.vuepress/dist gitlink.'
Write-Host 'The script intentionally initializes only the legitimate nested lebai-sdk.rs/proto/lebai-proto module.'
