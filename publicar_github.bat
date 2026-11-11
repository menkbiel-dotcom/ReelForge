@echo off
setlocal
pushd "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" publicar_github.py %*
pause
popd
