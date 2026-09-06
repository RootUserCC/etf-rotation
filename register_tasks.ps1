$ErrorActionPreference = 'Stop'

# 开机自启网站服务器：用 base 解释器的 pythonw.exe（真 GUI 程序，无窗口）
# 运行 run_server.pyw，由它把 .venv 的 site-packages 加入 sys.path 后执行 server.py。
# 注意不要用 .venv\Scripts\pythonw.exe —— 那是 uv 转发器，会拉起控制台版 python.exe
# 子进程并弹出终端窗口，关闭窗口会杀掉服务器。
$root = $PSScriptRoot
$pythonw = Join-Path $env:APPDATA 'uv\python\cpython-3.13-windows-x86_64-none\pythonw.exe'
$webAction = New-ScheduledTaskAction -Execute $pythonw -Argument 'run_server.pyw' -WorkingDirectory $root
$webTrigger = New-ScheduledTaskTrigger -AtLogOn
$webSettings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'ETFRotationWeb' -Action $webAction -Trigger $webTrigger -Settings $webSettings -Force | Out-Null

# 工作日收盘后更新数据：经 cmd /c 调用 bat（计划任务直接执行 .bat 不可靠），
# 并显式指定工作目录
$updateAction = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$root\定时更新.bat`"" -WorkingDirectory $root
$updateTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '15:10'
Register-ScheduledTask -TaskName 'ETFRotationDataUpdate' -Action $updateAction -Trigger $updateTrigger -Force | Out-Null

Get-ScheduledTask -TaskName 'ETFRotationWeb','ETFRotationDataUpdate' | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
