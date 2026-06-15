@echo off
chcp 65001 >nul
REM TinecmaTools Usage Example - Batch Script
REM Windows Command Prompt Version

title TinecmaTools - Usage Example

echo ========================================
echo   TinecmaTools Usage Example
echo ========================================
echo.

REM Configuration
set RENDERDOC_PATH=C:\Program Files\RenderDoc
set GAME_PATH=C:\Games\YourGame\game.exe
set OUTPUT_DIR=C:\Temp\TinecmaTools

echo [Configuration]
echo   RenderDoc Path: %RENDERDOC_PATH%
echo   Game Path: %GAME_PATH%
echo   Output Dir: %OUTPUT_DIR%
echo.

:MENU
echo [Select Operation Mode]
echo 1. Auto Mode (Recommended) - Complete all steps automatically
echo 2. Manual Mode - Execute each step manually
echo 3. Prepare Only - Rename and modify files only
echo 4. View Help Documents
echo 5. Exit
echo.

set /p CHOICE="Please enter option (1-5): "

if "%CHOICE%"=="1" goto AUTO_MODE
if "%CHOICE%"=="2" goto MANUAL_MODE
if "%CHOICE%"=="3" goto PREPARE_ONLY
if "%CHOICE%"=="4" goto HELP_DOCS
if "%CHOICE%"=="5" goto EXIT
goto INVALID

:AUTO_MODE
echo.
echo [Auto Mode]
echo Running auto bypass process...

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python not found, please install Python 3.7+
    pause
    exit /b 1
)

echo [+] Python found
echo [*] Starting auto tool...

python tools\auto_bypass.py --renderdoc "%RENDERDOC_PATH%" --game "%GAME_PATH%" --output "%OUTPUT_DIR%" --delay 10

goto END

:MANUAL_MODE
echo.
echo [Manual Mode]
echo.

REM Step 1
echo --- Step 1/4: Rename Files ---
set /p CONFIRM="Execute rename? (Y/N): "
if /i "%CONFIRM%"=="Y" (
    powershell -ExecutionPolicy Bypass -File tools\rename_tool.ps1 -RenderDocPath "%RENDERDOC_PATH%" -NewName "TinecmaTools"
)

REM Step 2
echo.
echo --- Step 2/4: Modify Feature Strings ---
set /p CONFIRM="Modify DLL strings? (Y/N): "
if /i "%CONFIRM%"=="Y" (
    if exist "%RENDERDOC_PATH%\TinecmaTools.dll" (
        python tools\string_replacer.py "%RENDERDOC_PATH%\TinecmaTools.dll"
    ) else (
        echo [Warning] DLL file not found, skipping this step
    )
)

REM Step 3
echo.
echo --- Step 3/4: Start Game ---
set /p CONFIRM="Start game? (Y/N): "
if /i "%CONFIRM%"=="Y" (
    if exist "%GAME_PATH%" (
        start "" "%GAME_PATH%"
        echo [+] Game started
    ) else (
        echo [Error] Game path not found: %GAME_PATH%
    )
)

REM Step 4
echo.
echo --- Step 4/4: Inject DLL ---
set /p CONFIRM="Inject DLL? (Y/N): "
if /i "%CONFIRM%"=="Y" (
    set /p PROCESS_NAME="Enter game process name (without .exe): "
    if exist "%RENDERDOC_PATH%\TinecmaTools.dll" (
        powershell -ExecutionPolicy Bypass -File tools\inject_dll.ps1 -ProcessName "%PROCESS_NAME%" -DllPath "%RENDERDOC_PATH%\TinecmaTools.dll" -Delay 5
    ) else (
        echo [Error] DLL file not found
    )
)

echo.
echo [Complete] Manual process finished
goto END

:PREPARE_ONLY
echo.
echo [Prepare Only]

echo [*] Renaming files...
powershell -ExecutionPolicy Bypass -File tools\rename_tool.ps1 -RenderDocPath "%RENDERDOC_PATH%" -NewName "TinecmaTools"

echo.
echo [*] Modifying feature strings...
if exist "%RENDERDOC_PATH%\TinecmaTools.dll" (
    python tools\string_replacer.py "%RENDERDOC_PATH%\TinecmaTools.dll"
)

echo.
echo [Complete] Tools prepared, ready for manual use
echo [Tip] Use TinecmaToolsUI.exe to start the interface
goto END

:HELP_DOCS
echo.
echo [Help Documents]
echo.
echo Available documents:
echo 1. README.md             - Main documentation
echo 2. QUICK_START.md        - Quick start guide
echo 3. bypass_anticheat_guide.md - Detailed technical documentation
echo 4. CHANGELOG.md          - Change log
echo 5. PROJECT_STRUCTURE.md  - Project structure
echo.

set /p DOC_CHOICE="Select document to view (1-5): "

if "%DOC_CHOICE%"=="1" notepad README.md
if "%DOC_CHOICE%"=="2" notepad QUICK_START.md
if "%DOC_CHOICE%"=="3" notepad bypass_anticheat_guide.md
if "%DOC_CHOICE%"=="4" notepad CHANGELOG.md
if "%DOC_CHOICE%"=="5" notepad PROJECT_STRUCTURE.md

goto END

:INVALID
echo.
echo [Error] Invalid option
pause
goto MENU

:EXIT
echo.
echo Goodbye!
timeout /t 2 >nul
exit /b 0

:END
echo.
echo ========================================
echo   Thank you for using TinecmaTools!
echo ========================================
pause
