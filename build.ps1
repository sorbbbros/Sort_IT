$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if ($LASTEXITCODE) { throw "Dependency installation failed" }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name SortIT --add-data "app_resources;app_resources" --collect-all pillow_heif launcher.py
if ($LASTEXITCODE) { throw "Build failed" }
Write-Host "Ready: dist\SortIT.exe"
