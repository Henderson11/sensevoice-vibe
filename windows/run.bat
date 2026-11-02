@echo off
rem 无窗口后台运行（计划任务/开机自启用）。日志在同目录 winvoice.log
cd /d "%~dp0"
start "" "%~dp0py312\pythonw.exe" "%~dp0winvoice.py"
