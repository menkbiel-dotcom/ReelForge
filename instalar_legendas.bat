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
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import json; from pathlib import Path; from faster_whisper import WhisperModel; m = json.loads(Path('settings.json').read_text(encoding='utf-8')).get('whisper_model', 'small') if Path('settings.json').exists() else 'small'; print('Baixando modelo Whisper (' + m + ')...'); WhisperModel(m, device='cpu', compute_type='int8', download_root='output/alignment_cache/models')"
if errorlevel 1 (
  echo Nao foi possivel baixar o modelo do Whisper. Confira a conexao e tente novamente.
) else (
  echo Pronto. Selecione auto ou whisper na aba Vozes e legendas.
)
pause
popd
