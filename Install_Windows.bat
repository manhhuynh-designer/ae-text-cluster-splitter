@echo off
cd /d "%~dp0"
echo ==========================================================
echo   Cai dat AE Text Cluster ^& Mask Splitter cho Windows
echo ==========================================================

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [LOI] Khong tim thay Python! Vui long cai Python 3 tu python.org ^(tich chon Add Python to PATH^).
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Dang tao moi truong Python ^(.venv^)...
    python -m venv .venv
)

echo Dang cai dat thu vien xu ly anh ^& ONNXRuntime ^(opencv, numpy, scipy, pillow, onnxruntime^)...
.\.venv\Scripts\python.exe -m pip install --upgrade pip -q
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -q

for %%v in (9 10 11 12 13) do (
    reg add "HKCU\Software\Adobe\CSXS.%%v" /v PlayerDebugMode /t REG_SZ /d "1" /f >nul 2>nul
)

.\.venv\Scripts\python.exe text_cluster_splitter.py --install-only

echo.
echo [HOAN TAT] Da cai dat Plugin vao After Effects thanh cong!
echo 1. Trong After Effects, vao Edit ^> Preferences ^> Scripting ^& Expressions:
echo    Tich chon "Allow Scripts to Write Files and Access Network".
echo 2. Mo Plugin tai menu: Window ^> AE_Text_Cluster_Splitter.jsx
echo    hoac: Window ^> Extensions ^> Text Cluster Splitter
pause
