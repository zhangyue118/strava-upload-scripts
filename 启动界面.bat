@echo off
cd /d "%~dp0"

where pythonw >nul 2>&1
if errorlevel 1 (
    echo.
    echo   找不到 pythonw。说明这台电脑没装 Python，
    echo   或者装的时候没有勾选 "Add Python to PATH"。
    echo.
    echo   请到 https://www.python.org/downloads/ 下载安装，
    echo   安装第一步务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

start "" pythonw "strava_gui.pyw"
