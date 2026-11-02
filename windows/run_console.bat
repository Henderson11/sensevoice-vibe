@echo off
rem 前台运行（带控制台输出，调试用）；Ctrl+C 退出
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0py312\python.exe" winvoice.py
pause
