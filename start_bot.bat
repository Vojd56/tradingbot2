@echo off
cd /d "%~dp0"
title Discord Trading Bot (AIBot 2.0)

echo =======================================================
echo   SPUSTENI AUTONOMNIHO DISCORD TRADING BOTA
echo =======================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [CHYBA] Python nebyl nalezen v systemu PATH!
        echo Nainstalujte prosim Python z https://www.python.org a zaskrtnete 'Add Python to PATH'.
        echo.
        pause
        exit /b 1
    )
    set PY_CMD=py -3
) else (
    set PY_CMD=python
)

echo Spoustim bota pomoci: %PY_CMD% bot.py ...
echo.
%PY_CMD% bot.py

if %errorlevel% neq 0 (
    echo.
    echo =======================================================
    echo [CHYBA] Bot byl neocekavane ukoncen s chybou.
    echo =======================================================
    echo.
    pause
)
