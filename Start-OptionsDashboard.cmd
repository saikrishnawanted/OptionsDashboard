@echo off
title Options Dashboard
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-OptionsDashboard.ps1"
if errorlevel 1 pause
