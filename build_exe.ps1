# Build a single shareable FLINT.exe.
#
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Output: dist\FLINT.exe  (copy this one file to anyone — it is self-contained,
# except Playwright/Chromium browser automation, which needs `playwright install`
# on the target machine).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Prefer the project venv's Python if present.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "==> Installing build dependencies..." -ForegroundColor Cyan
& $py -m pip install -r requirements-build.txt

Write-Host "==> Cleaning previous build..." -ForegroundColor Cyan
if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
if (Test-Path "dist")  { Remove-Item "dist"  -Recurse -Force }

Write-Host "==> Building FLINT (one-folder; this can take several minutes)..." -ForegroundColor Cyan
& $py -m PyInstaller flint.spec --noconfirm

$folder = Join-Path $root "dist\FLINT"
$exe    = Join-Path $folder "FLINT.exe"
if (-not (Test-Path $exe)) {
    Write-Host "==> Build finished but dist\FLINT\FLINT.exe was not found." -ForegroundColor Red
    Write-Host "    If the folder is empty, Defender likely quarantined it — add an" -ForegroundColor Red
    Write-Host "    exclusion for this folder and rebuild:" -ForegroundColor Red
    Write-Host "      Add-MpPreference -ExclusionPath '$root'   (run as Administrator)" -ForegroundColor Red
    exit 1
}

Write-Host "==> Zipping for sharing..." -ForegroundColor Cyan
$zip = Join-Path $root "dist\FLINT.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $folder -DestinationPath $zip -Force

$size = "{0:N1} MB" -f ((Get-Item $zip).Length / 1MB)
Write-Host "==> Done." -ForegroundColor Green
Write-Host "    Folder: $folder\FLINT.exe" -ForegroundColor Green
Write-Host "    Share : $zip ($size)" -ForegroundColor Green
Write-Host "    Recipients unzip and run FLINT.exe; first launch asks for" -ForegroundColor Green
Write-Host "    language + a free account." -ForegroundColor Green
