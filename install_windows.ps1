$ErrorActionPreference = "Stop"

if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"
    $pythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
    $pythonArgs = @()
} else {
    throw "Python 3 is required. Install it from https://www.python.org/downloads/ and try again."
}

& $python @pythonArgs -m venv .venv
$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host ""
Write-Host "Installation complete."
Write-Host "Start the app with: .\.venv\Scripts\python.exe run.py"
