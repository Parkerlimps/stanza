@echo off
chcp 65001 >nul
setlocal
rem 解释器优先级：STANZA_PYTHON > 同目录 python.local.txt（本机私有，不提交） > PATH 里的 python
set "PY=%STANZA_PYTHON%"
if not defined PY if exist "%~dp0python.local.txt" set /p PY=<"%~dp0python.local.txt"
if not defined PY set "PY=python"
cd /d "%~dp0"
"%PY%" main.py %*
endlocal
