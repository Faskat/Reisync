@echo off
rem Reisync launcher: uses .venv if present, otherwise system python
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" main.py
rem keep the window open on crash so the error is visible
if errorlevel 1 pause
