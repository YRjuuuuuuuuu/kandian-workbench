@echo off
chcp 65001 >nul
if exist "%~dp0看点工场.exe" (
  start "" "%~dp0看点工场.exe"
) else (
  echo 未找到看点工场.exe，请先运行 scripts\build_launcher.ps1。
  pause
)
