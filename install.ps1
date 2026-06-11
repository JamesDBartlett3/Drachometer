$ErrorActionPreference = 'Stop'

$repo = if ($env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_REPO) {
    $env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_REPO
} else {
    'JamesDBartlett3/claude-code-token-usage-dashboard'
}

$releasesApi = if ($env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_RELEASES_API) {
    $env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_RELEASES_API
} else {
    "https://api.github.com/repos/$repo/releases/latest"
}

$assetName = if ($env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ASSET_NAME) {
    $env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ASSET_NAME
} else {
    'claude-code-token-usage-dashboard.zip'
}

$archiveUrl = $env:CLAUDE_CODE_TOKEN_USAGE_DASHBOARD_ARCHIVE_URL
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("claude-code-token-usage-dashboard-" + [System.Guid]::NewGuid().ToString('N'))
$archivePath = Join-Path $tempRoot $assetName

function Find-Python {
    foreach ($candidate in @('py', 'python', 'python3')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            return $candidate
        }
    }

    throw 'Python 3.10+ is required.'
}

function Get-LocalPath([string] $Value) {
    if (-not $Value) {
        return $null
    }

    if ($Value.StartsWith('file://')) {
        return $Value.Substring(7)
    }

    if (Test-Path $Value) {
        return $Value
    }

    return $null
}

function Read-ReleaseMetadata([string] $Source) {
    $localPath = Get-LocalPath $Source
    if ($localPath) {
        return Get-Content -Path $localPath -Raw | ConvertFrom-Json
    }

    return Invoke-RestMethod -Uri $Source -Headers @{ Accept = 'application/vnd.github+json' }
}

function Resolve-ArchiveUrl($Release, [string] $ExpectedName) {
    $asset = $Release.assets | Where-Object { $_.name -eq $ExpectedName } | Select-Object -First 1
    if (-not $asset) {
        $asset = $Release.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1
    }

    if (-not $asset -or -not $asset.browser_download_url) {
        throw "No release zip asset was found in $releasesApi."
    }

    return $asset.browser_download_url
}

try {
    New-Item -ItemType Directory -Path $tempRoot | Out-Null

    if (-not $archiveUrl) {
        Write-Host "Looking up latest release for $repo..."
        $release = Read-ReleaseMetadata $releasesApi
        $archiveUrl = Resolve-ArchiveUrl $release $assetName
    }

    Write-Host 'Downloading release asset...'
    $localArchive = Get-LocalPath $archiveUrl
    if ($localArchive) {
        Copy-Item -Path $localArchive -Destination $archivePath
    } else {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $archivePath
    }

    Write-Host 'Extracting installer...'
    Expand-Archive -Path $archivePath -DestinationPath $tempRoot -Force

    $installer = Get-ChildItem -Path $tempRoot -Filter install.py -Recurse | Select-Object -First 1
    if (-not $installer) {
        throw 'install.py was not found in the downloaded archive.'
    }

    $python = Find-Python
    Write-Host "Running installer with $python..."
    & $python $installer.FullName
} finally {
    if (Test-Path $tempRoot) {
        Remove-Item -Path $tempRoot -Recurse -Force
    }
}
