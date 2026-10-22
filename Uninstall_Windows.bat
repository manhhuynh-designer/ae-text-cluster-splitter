@echo off
setlocal EnableDelayedExpansion
rem ==========================================================
rem  Go cai dat AE Text Cluster ^& Mask Splitter cho Windows
rem  - Khong can Python (van chay duoc khi .venv da bi xoa)
rem ==========================================================
cd /d "%~dp0"
echo ==========================================================
echo   Go cai dat AE Text Cluster ^& Mask Splitter (Windows)
echo ==========================================================
echo.
echo Hay dong After Effects truoc khi go (de AE khong giu file panel).
set /p ANS="Tiep tuc go cai dat? (y/N): "
if /i not "%ANS%"=="y" (
    echo Da huy.
    pause
    exit /b 0
)

set REMOVED=0

rem 1. Dung server nen (python/pythonw dang chay text_cluster_splitter.py)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*text_cluster_splitter.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('[OK] Da dung server nen PID ' + $_.ProcessId) }" 2>nul

rem 2. Xoa ScriptUI Panel / Script trong moi phien ban After Effects
if exist "%APPDATA%\Adobe\After Effects" (
    for /d %%V in ("%APPDATA%\Adobe\After Effects\2*") do (
        if exist "%%~V\Scripts\AE_Text_Cluster_Splitter.jsx" (
            del /f /q "%%~V\Scripts\AE_Text_Cluster_Splitter.jsx"
            echo [XOA] %%~V\Scripts\AE_Text_Cluster_Splitter.jsx
            set /a REMOVED+=1
        )
        if exist "%%~V\Scripts\ScriptUI Panels\AE_Text_Cluster_Splitter.jsx" (
            del /f /q "%%~V\Scripts\ScriptUI Panels\AE_Text_Cluster_Splitter.jsx"
            echo [XOA] %%~V\Scripts\ScriptUI Panels\AE_Text_Cluster_Splitter.jsx
            set /a REMOVED+=1
        )
    )
)

rem 3. Xoa CEP Extension
set "CEP_DIR=%APPDATA%\Adobe\CEP\extensions\com.manh.textclustersplitter"
if exist "%CEP_DIR%" (
    rmdir /s /q "%CEP_DIR%"
    echo [XOA] %CEP_DIR%
    set /a REMOVED+=1
)

rem 4. Xoa file tam
del /f /q "%TEMP%\ae_splitter_*" >nul 2>nul
echo [DON] Da xoa file tam ae_splitter_*

rem 5. (Tuy chon) Tat PlayerDebugMode cua Adobe CEP
echo.
echo PlayerDebugMode dung chung cho MOI extension CEP chua ky (khong chi plugin nay).
set /p DBG="Tat PlayerDebugMode? Chi chon y neu ban khong dung extension CEP nao khac (y/N): "
if /i "%DBG%"=="y" (
    for %%v in (9 10 11 12 13) do (
        reg delete "HKCU\Software\Adobe\CSXS.%%v" /v PlayerDebugMode /f >nul 2>nul
    )
    echo [OK] Da tat PlayerDebugMode
)

rem 6. (Tuy chon) Xoa moi truong Python + model ONNX trong thu muc nay
echo.
set /p PURGE="Xoa .venv va model ONNX (~45 MB) trong thu muc nay? (y/N): "
if /i "%PURGE%"=="y" (
    if exist ".venv" rmdir /s /q ".venv"
    if exist "__pycache__" rmdir /s /q "__pycache__"
    del /f /q "models\*.onnx" "models\*.tmp" >nul 2>nul
    echo [OK] Da xoa .venv, __pycache__ va model ONNX
)

echo.
echo [HOAN TAT] Da go cai dat (xoa !REMOVED! muc khoi After Effects).
echo Mo lai After Effects de menu Window khong con hien plugin.
echo Muon xoa han, ban co the xoa ca thu muc nay: %CD%
pause
endlocal
