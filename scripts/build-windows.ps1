[CmdletBinding()]
param(
    [string]$Python = "py",
    [string]$OutputDirectory = "artifacts/windows"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$OutputPath = Join-Path $ProjectRoot $OutputDirectory
$BuildPath = Join-Path $ProjectRoot "build/pyinstaller"

Set-Location $ProjectRoot

$PythonArguments = @()
if ($Python -eq "py") {
    $PythonArguments = @("-3")
}

& $Python @PythonArguments -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "VitaMediaDeck-Transcoder" `
    --distpath $OutputPath `
    --workpath $BuildPath `
    --specpath $BuildPath `
    vitamediadeck_tui.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$Executable = Join-Path $OutputPath "VitaMediaDeck-Transcoder.exe"
if (-not (Test-Path -Path $Executable -PathType Leaf)) {
    throw "Expected executable was not created: $Executable"
}

Get-Item $Executable | Select-Object FullName, Length, LastWriteTime
