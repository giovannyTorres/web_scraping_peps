param([string]$Python = 'python', [string]$Destination = 'dist')
$ErrorActionPreference = 'Stop'
$RepoPath = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $RepoPath
try {
    & $Python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw 'Pruebas fallidas; no se construye el ejecutable.' }
    & $Python -m PyInstaller --noconfirm --clean --onedir --name PEPS-MX --windowed --noupx --collect-data peps --collect-submodules peps --distpath $Destination launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Falló PyInstaller' }
    & $Python -m PyInstaller --noconfirm --onedir --name PEPS-CLI --console --noupx --collect-data peps --collect-submodules peps --distpath $Destination launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Falló CLI PyInstaller' }
    Copy-Item -LiteralPath (Join-Path $Destination 'PEPS-CLI/PEPS-CLI.exe') -Destination (Join-Path $Destination 'PEPS-MX/PEPS-CLI.exe') -Force
} finally { Pop-Location }
