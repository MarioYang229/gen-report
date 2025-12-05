@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

echo ===== Auto Weekly Report Generator =====
echo.

:: Change to program directory first
cd /d "%~dp0"

:: Set base path
set "BASE_PATH=\\Fileserver\智能控制部\【h_文件】定期報告\週報"

echo Using base path: %BASE_PATH%

if not exist "%BASE_PATH%" (
    echo Error: Base path not found: %BASE_PATH%
    pause
    exit /b 1
)

echo Searching for latest date folder...

:: Find the latest date folder (format: MMDD)
set "LATEST_DATE="
set "LATEST_PATH="

:: Use PowerShell to get directory list and process in batch
for /f "usebackq delims=" %%d in (`powershell -Command "Get-ChildItem '%BASE_PATH%' -Directory | Where-Object {$_.Name -match '^[0-9]{4}$'} | Sort-Object Name | Select-Object -Last 1 -ExpandProperty Name"`) do (
    set "LATEST_DATE=%%d"
    set "LATEST_PATH=%BASE_PATH%\%%d"
)

if "%LATEST_DATE%"=="" (
    echo Error: Cannot find any date folders with MMDD format
    pause
    exit /b 1
)

echo Found latest date folder: %LATEST_DATE%
echo Path: %LATEST_PATH%
echo.

:: Check for work report files
echo Checking for work report files...
set "FOUND_REPORTS=0"

dir "%LATEST_PATH%\*工作報告-*.md" "%LATEST_PATH%\*工作報告-*.docx" "%LATEST_PATH%\*工作報告-*.doc" 2>nul | find "工作報告" >nul
if %errorlevel% equ 0 (
    echo Found work report files in %LATEST_PATH%
    set "FOUND_REPORTS=1"
    dir "%LATEST_PATH%\*工作報告-*.*" /b 2>nul
)

if %FOUND_REPORTS%==0 (
    echo Warning: No work report files found in %LATEST_PATH%
    echo Please check the source directory
    set /p "CONTINUE=Continue anyway? (Y/N): "
    if /i "!CONTINUE!" neq "Y" (
        exit /b 1
    )
)

echo.
echo Generating weekly report...

set "OUTPUT_FILE=%LATEST_PATH%\工作報告匯總.md"
echo Output file: %OUTPUT_FILE%
echo Command: uv run python -m gen_report --source "%LATEST_PATH%" --out "%OUTPUT_FILE%"
echo.

uv run python -m gen_report --source "%LATEST_PATH%" --out "%OUTPUT_FILE%"

if %errorlevel% equ 0 (
    echo.
    echo ===== SUCCESS! =====
    echo Weekly report generated at: %OUTPUT_FILE%
    echo.

    set /p "OPEN_FILE=Open the generated report? (Y/N): "
    if /i "!OPEN_FILE!"=="Y" (
        start "" "%OUTPUT_FILE%"
    )
) else (
    echo.
    echo ===== ERROR! =====
    echo Report generation failed - check error messages above
)

echo.