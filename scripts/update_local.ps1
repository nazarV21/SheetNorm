$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$Candidates = @(
    (Join-Path $ProjectRoot "instance\sheetnorm.db"),
    (Join-Path $ProjectRoot "sheetnorm.db")
)

foreach ($Database in $Candidates) {
    if (Test-Path $Database) {
        $Backup = "$Database.backup-$Timestamp"
        Copy-Item -LiteralPath $Database -Destination $Backup -Force
        Write-Host "Database backup: $Backup" -ForegroundColor Yellow
    }
}

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . ".\.venv\Scripts\Activate.ps1"
}

python -m pip install -r requirements.txt
if (Test-Path "requirements-ai.txt") {
    python -m pip install -r requirements-ai.txt
}

flask --app main repair-local-db
flask --app main db upgrade
flask --app main db-schema-status

Write-Host "SheetNorm database and dependencies were updated successfully." -ForegroundColor Green
