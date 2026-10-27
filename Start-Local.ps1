$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$cachedPackages = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\work\python312'))
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython scripts\start.py
} elseif ((Test-Path -LiteralPath $bundledPython) -and (Test-Path -LiteralPath $cachedPackages)) {
    $priorPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $cachedPackages
        & $bundledPython scripts\start.py
    } finally {
        $env:PYTHONPATH = $priorPythonPath
    }
} else {
    Write-Error 'Create a Python 3.12 .venv and install requirements-dev.lock as described in README.md.'
}
