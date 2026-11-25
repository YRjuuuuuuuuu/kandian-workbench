$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $taskCompiler)) { $taskCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $taskCompiler)) { throw '未找到 Windows .NET Framework 编译器' }
$taskOutput = Join-Path $taskRoot '看点工场.exe'
& $taskCompiler /nologo /target:winexe /optimize+ /utf8output /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll /out:$taskOutput (Join-Path $taskRoot 'launcher\WorkbenchLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw '控制台编译失败' }
Write-Output ('已生成：' + $taskOutput)
