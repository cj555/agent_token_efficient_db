@echo off
setlocal enabledelayedexpansion
rem 计划任务的通用包装：cd 到仓库根 + 追加日志 + 调本仓库的 python。
rem 存在的理由是 schtasks /tr 有 261 字符上限，内联 cd+echo+重定向的完整命令行
rem 对本仓库路径长度会超限，所以把这部分固定逻辑挪进这个脚本，schtasks 只传短参数。
rem 用法：run_logged.cmd <日志名，通常等于任务名> <传给 python.exe 的其余参数...>
rem   dwlib 子命令： run_logged.cmd dw-family-example -m dwlib.cli run --family example
rem   独立脚本：     run_logged.cmd dw-monitor scripts\monitor_sources.py
set REPO=%~dp0..
set TASKNAME=%1
set LOG=%REPO%\logs\%TASKNAME%.log
if not exist "%REPO%\logs" mkdir "%REPO%\logs"

set ARGS=
:loop
shift
if "%~1"=="" goto after
set ARGS=!ARGS! %1
goto loop
:after

echo ==== %DATE% %TIME% ==== >> "%LOG%"
cd /d "%REPO%"
"%REPO%\.venv\Scripts\python.exe" !ARGS! >> "%LOG%" 2>&1
