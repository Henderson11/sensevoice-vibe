@echo off
rem 停止后台运行的 WinVoice
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" | Where-Object { $_.CommandLine -like '*winvoice.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo stopped
