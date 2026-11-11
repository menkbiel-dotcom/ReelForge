@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Execute iniciar_windows.bat antes de instalar as legendas por audio.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-legendas.txt
if errorlevel 1 (
  echo Nao foi possivel instalar. Confira a conexao e tente novamente.
) else (
  echo Pronto. Selecione auto ou whisper na aba Vozes e legendas.
  echo O primeiro uso baixa o modelo escolhido.
)
pause
popd
