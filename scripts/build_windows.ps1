param([string]$Python = 'python', [string]$Destination = 'dist')
$ErrorActionPreference = 'Stop'

function Test-EquivalentBaseLibrary([string]$Source, [string]$Target) {
    # PyInstaller may emit identical modules in a different ZIP entry order.
    # Compare their contents and retain the GUI archive when they agree.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $SourceZip = [IO.Compression.ZipFile]::OpenRead($Source)
    try {
        $TargetZip = [IO.Compression.ZipFile]::OpenRead($Target)
        try {
            if ($SourceZip.Entries.Count -ne $TargetZip.Entries.Count) { return $false }
            foreach ($Entry in $SourceZip.Entries) {
                $Other = $TargetZip.GetEntry($Entry.FullName)
                if ($null -eq $Other -or $Entry.Length -ne $Other.Length) { return $false }
                $SourceStream = $Entry.Open()
                $TargetStream = $Other.Open()
                try {
                    if ((Get-FileHash -InputStream $SourceStream -Algorithm SHA256).Hash -ne
                        (Get-FileHash -InputStream $TargetStream -Algorithm SHA256).Hash) { return $false }
                } finally {
                    $SourceStream.Dispose()
                    $TargetStream.Dispose()
                }
            }
            return $true
        } finally { $TargetZip.Dispose() }
    } finally { $SourceZip.Dispose() }
}

function Merge-SharedRuntime([string]$Source, [string]$Target) {
    $Source = (Resolve-Path -LiteralPath $Source).Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
        throw "Falta el runtime gráfico: $Target"
    }
    foreach ($File in Get-ChildItem -LiteralPath $Source -Recurse -File) {
        $Relative = $File.FullName.Substring($Source.Length + 1)
        $SharedFile = Join-Path $Target $Relative
        if (Test-Path -LiteralPath $SharedFile) {
            # Never overwrite a different DLL or resource used by the GUI.
            $SourceHash = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash
            $TargetHash = (Get-FileHash -LiteralPath $SharedFile -Algorithm SHA256).Hash
            if ($SourceHash -ne $TargetHash) {
                if ($Relative -eq 'base_library.zip' -and
                    (Test-EquivalentBaseLibrary -Source $File.FullName -Target $SharedFile)) { continue }
                throw "Runtime CLI incompatible con la variante gráfica: $Relative"
            }
        } else {
            New-Item -ItemType Directory -Path (Split-Path -Parent $SharedFile) -Force | Out-Null
            Copy-Item -LiteralPath $File.FullName -Destination $SharedFile
        }
    }
}

$RepoPath = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $RepoPath
try {
    $Destination = [IO.Path]::GetFullPath($Destination)
    & $Python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw 'Pruebas fallidas; no se construye el ejecutable.' }
    & $Python -m PyInstaller --noconfirm --clean --onedir --contents-directory _internal --name PEPS-MX --windowed --python-option 'X utf8' --noupx --collect-data peps --collect-submodules peps --distpath $Destination launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Falló PyInstaller' }
    & $Python -m PyInstaller --noconfirm --onedir --contents-directory _internal --name PEPS-CLI --console --python-option 'X utf8' --noupx --collect-data peps --collect-submodules peps --distpath $Destination launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Falló CLI PyInstaller' }
    Merge-SharedRuntime -Source (Join-Path $Destination 'PEPS-CLI/_internal') -Target (Join-Path $Destination 'PEPS-MX/_internal')
    $PortableCLI = Join-Path $Destination 'PEPS-MX/PEPS-CLI.exe'
    Copy-Item -LiteralPath (Join-Path $Destination 'PEPS-CLI/PEPS-CLI.exe') -Destination $PortableCLI -Force
    & $Python -I scripts/validate_distribution.py --executable $PortableCLI --output (Join-Path $Destination 'windows-distribution-validation.json')
    if ($LASTEXITCODE -ne 0) { throw 'Falló la validación del paquete portable final.' }
} finally { Pop-Location }
