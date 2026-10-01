@echo off
title Stop Options Dashboard
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-OptionsDashboard.ps1"
pause
