param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path $PSScriptRoot).Path.TrimEnd("\")
$ProjectName = Split-Path $ProjectRoot -Leaf
$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $ParentDirectory = Split-Path $ProjectRoot -Parent
    $OutputPath = Join-Path $ParentDirectory "$ProjectName-source-$Timestamp.zip"
}
elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $ProjectRoot $OutputPath
}

$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-pack-$([guid]::NewGuid().ToString('N'))"
$StagingRoot = Join-Path $TempRoot $ProjectName

$ExcludedDirectories = @(
    ".git",
    ".github",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    ".tox",
    ".nox",
    ".playwright-mcp",
    ".agents",
    "node_modules",
    "logs",
    "input",
    "output",
    "storage",
    "dist",
    "build"
)

$ExcludedFileNames = @(
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    "sheetnorm.db",
    "history.json",
    "rules.json",
    "instruction_feedback.json",
    "training_examples.json",
    "coverage.xml",
    ".coverage",
    "pack_project.ps1"
)

$ExcludedPatterns = @(
    "*.gguf",
    "*.zip",
    "*.7z",
    "*.rar",
    "*.tar",
    "*.gz",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.log",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.bak",
    "*.tmp",
    "*.temp",
    "*.coverage"
)

function Test-ExcludedFile {
    param(
        [string]$RelativePath,
        [System.IO.FileInfo]$File
    )

    $NormalizedPath = $RelativePath.Replace("\", "/")
    $PathParts = $NormalizedPath.Split("/", [System.StringSplitOptions]::RemoveEmptyEntries)

    foreach ($Part in $PathParts) {
        if ($ExcludedDirectories -contains $Part) {
            return $true
        }
    }

    if ($ExcludedFileNames -contains $File.Name) {
        return $true
    }

    foreach ($Pattern in $ExcludedPatterns) {
        if ($File.Name -like $Pattern) {
            return $true
        }
    }

    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($File.FullName, $OutputPath)) {
        return $true
    }

    return $false
}

try {
    New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null

    $IncludedFiles = 0
    $ExcludedFiles = 0
    $IncludedBytes = [int64]0

    Get-ChildItem -Path $ProjectRoot -File -Recurse -Force | ForEach-Object {
        $File = $_

        $RelativePath = $File.FullName.Substring($ProjectRoot.Length)
        $RelativePath = $RelativePath.TrimStart([char[]]@("\", "/"))

        if (Test-ExcludedFile -RelativePath $RelativePath -File $File) {
            $ExcludedFiles++
            return
        }

        $Destination = Join-Path $StagingRoot $RelativePath
        $DestinationDirectory = Split-Path $Destination -Parent

        if (-not (Test-Path $DestinationDirectory)) {
            New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
        }

        Copy-Item -LiteralPath $File.FullName -Destination $Destination -Force

        $IncludedFiles++
        $IncludedBytes += $File.Length
    }

    if ($IncludedFiles -eq 0) {
        throw "No files were selected for the archive."
    }

    $OutputDirectory = Split-Path $OutputPath -Parent

    if (-not (Test-Path $OutputDirectory)) {
        New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    }

    if (Test-Path $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem

    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $TempRoot,
        $OutputPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $Archive = Get-Item $OutputPath
    $SourceSizeMb = [math]::Round($IncludedBytes / 1MB, 2)
    $ArchiveSizeMb = [math]::Round($Archive.Length / 1MB, 2)

    Write-Host ""
    Write-Host "Archive created successfully." -ForegroundColor Green
    Write-Host "Path:      $OutputPath"
    Write-Host "Included:  $IncludedFiles files"
    Write-Host "Excluded:  $ExcludedFiles files"
    Write-Host "Source:    $SourceSizeMb MB"
    Write-Host "ZIP:       $ArchiveSizeMb MB"
    Write-Host ""
    Write-Host "Excluded: virtual environments, caches, logs, input/output/storage,"
    Write-Host "local databases, runtime JSON files, .env secrets, GGUF models, archives."
}
finally {
    if (Test-Path $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force
    }
}
