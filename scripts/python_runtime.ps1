param([switch]$FullInstallation)

$taskRoot = Split-Path -Parent $PSScriptRoot
$taskBundled = Join-Path $taskRoot 'runtime\python.exe'
if ($env:CONTENT_ENGINE_PYTHON) {
    $taskPython = $env:CONTENT_ENGINE_PYTHON
} elseif (!$FullInstallation -and (Test-Path -LiteralPath $taskBundled)) {
    $taskPython = $taskBundled
} else {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
& $taskPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw '请安装 Python 3.11+，或使用内置 Python 的 Windows 独立版' }
Write-Output $taskPython
