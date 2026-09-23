@echo off
setlocal
cd /d "%~dp0"

if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Removing existing PyTorch packages...
%PY% -m pip uninstall -y torch torchvision torchaudio
if errorlevel 1 exit /b %errorlevel%

echo Installing PyTorch CUDA 13.2 wheels...
%PY% -m pip install torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cu132
