@echo off
rem Install / refresh the VISTA visual harness plugin into the current DSH profile.
rem Local .ps1 files are blocked by the RemoteSigned execution policy in some
rem setups, so the wrapper launches Windows PowerShell with a per-process bypass.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
