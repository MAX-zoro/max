$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$toolPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $toolPython)) {
    $installedPython = Get-Command python -ErrorAction SilentlyContinue
    $bundledPython = 'C:\Users\Max\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if ($installedPython) { $setupPython = $installedPython.Source }
    elseif (Test-Path -LiteralPath $bundledPython) { $setupPython = $bundledPython }
    else { throw '请先安装 Python 3.12，然后重新运行。' }
    & $setupPython -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
}
& $toolPython -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
& $toolPython app.py
