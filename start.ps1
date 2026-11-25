param([int]$Port = 8765, [switch]$Background)
$ErrorActionPreference = 'Stop'
$taskPython = & (Join-Path $PSScriptRoot 'scripts/python_runtime.ps1')
$taskData = Join-Path $PSScriptRoot 'data'
if (-not (Test-Path -LiteralPath $taskData)) { New-Item -ItemType Directory -Path $taskData | Out-Null }
if ($Background) {
    $taskUrl = 'http://127.0.0.1:' + $Port
    try {
        $taskExistingHealth = Invoke-RestMethod -Uri ($taskUrl + '/health') -TimeoutSec 2 -ErrorAction Stop
        if ($taskExistingHealth.ok -and $taskExistingHealth.version -eq '0.9.2') {
            Write-Output ('工作台已在运行：' + $taskUrl)
            return
        }
        if ($taskExistingHealth.ok) { throw ('旧版服务仍在运行：' + $taskExistingHealth.version + '。请运行 升级并重启工作台.cmd；不会同时启动第二个服务。') }
    } catch { if ($_.Exception.Message -like '*旧版服务仍在运行*') { throw } }
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-X','utf8','-m','content_engine','serve','--port', $Port, '--data-dir', ('"' + $taskData + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskData 'server.log') -RedirectStandardError (Join-Path $taskData 'server-error.log')
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 12; $taskAttempt++) {
        Start-Sleep -Milliseconds 250
        $taskProcess.Refresh()
        if ($taskProcess.HasExited) { throw ('工作台启动失败；查看 ' + (Join-Path $taskData 'server-error.log')) }
        try {
            $taskHealth = Invoke-RestMethod -Uri ($taskUrl + '/health') -TimeoutSec 1 -ErrorAction Stop
            if ($taskHealth.ok) { $taskReady = $true; break }
        } catch { }
    }
    if (-not $taskReady) { throw ('启动健康检查未通过；查看 ' + (Join-Path $taskData 'server-error.log')) }
    Write-Output ('已启动后台进程 PID ' + $taskProcess.Id)
    Write-Output ('工作台：' + $taskUrl)
} else {
    Push-Location $PSScriptRoot
    try { & $taskPython -X utf8 -m content_engine serve --port $Port } finally { Pop-Location }
}
