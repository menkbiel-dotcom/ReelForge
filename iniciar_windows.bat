@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 goto path_error
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links" set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
if not exist "logs" mkdir "logs"
set "LOG=%CD%\logs\inicializacao.log"
>"%LOG%" echo ReelForge - %date% %time%
if errorlevel 1 goto path_error
if "%~1"=="--teste-erro" goto test_error
set "STAGE=Localizar Python 3.11 ou 3.12"
if exist ".venv\Scripts\python.exe" goto check_venv
if defined REELFORGE_PYTHON goto custom_python
py -3.12 --version >>"%LOG%" 2>&1
if not errorlevel 1 goto create_venv_312
py -3.11 --version >>"%LOG%" 2>&1
if not errorlevel 1 goto create_venv
python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)" >>"%LOG%" 2>&1
if not errorlevel 1 goto system_python
if not exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" goto missing_python
set "REELFORGE_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
:custom_python
"%REELFORGE_PYTHON%" -m venv .venv >>"%LOG%" 2>&1
if errorlevel 1 goto fail
goto check_venv
:system_python
python -m venv .venv >>"%LOG%" 2>&1
if errorlevel 1 goto fail
goto check_venv
:create_venv_312
set "STAGE=Criar ambiente virtual"
py -3.12 -m venv .venv >>"%LOG%" 2>&1
if errorlevel 1 goto fail
goto check_venv
:create_venv
set "STAGE=Criar ambiente virtual"
py -3.11 -m venv .venv >>"%LOG%" 2>&1
if errorlevel 1 goto fail
:check_venv
set "STAGE=Verificar ambiente virtual"
".venv\Scripts\python.exe" --version >>"%LOG%" 2>&1
if errorlevel 1 goto fail
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "STAGE=Validar dependencias"
".venv\Scripts\python.exe" -m pip check >>"%LOG%" 2>&1
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import tkinter, feedparser, requests, edge_tts, PIL, dotenv, openai, yt_dlp; t=tkinter.Tk(); t.withdraw(); t.destroy()" >>"%LOG%" 2>&1
if errorlevel 1 goto fail
if not exist "assets\gameplay" mkdir "assets\gameplay"
if not exist "output" mkdir "output"
".venv\Scripts\python.exe" -c "from media_tools import ffmpeg; print(ffmpeg())" >>"%LOG%" 2>&1
if errorlevel 1 goto fail
set "STAGE=Abrir interface"
if "%~1"=="--verificar" goto verified
echo Abrindo ReelForge. Mantenha esta janela aberta.
".venv\Scripts\python.exe" -u launcher.py >>"%LOG%" 2>&1
if errorlevel 1 goto fail
popd
exit /b 0
:verified
echo Ambiente verificado com sucesso.
popd
exit /b 0
:missing_python
>>"%LOG%" echo Python 3.11/3.12 nao encontrado.
echo Instale Python 3.12 com Tcl/Tk e pip, depois execute novamente.
echo Com o gerenciador Python disponivel, execute: py install 3.12
goto fail
:test_error
set "STAGE=Teste controlado de erro"
>>"%LOG%" echo Falha simulada para verificar log e pausa.
:fail
echo.
echo ERRO na etapa: %STAGE%
>>"%LOG%" echo ERRO na etapa: %STAGE%
type "%LOG%"
echo.
echo Log salvo em: "%LOG%"
if not defined REELFORGE_NO_PAUSE pause
popd
exit /b 1
:path_error
echo Nao foi possivel acessar a pasta do programa ou gravar logs.
if not defined REELFORGE_NO_PAUSE pause
exit /b 1


