param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$taskNames = @('ETFRotationWeb', 'ETFRotationDataUpdate')

if ($Uninstall) {
    foreach ($tn in $taskNames) {
        if (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $tn -Confirm:$false
            Write-Host "[已卸载] 计划任务 $tn" -ForegroundColor Yellow
        } else {
            Write-Host "[跳过] 未找到计划任务 $tn"
        }
    }
    exit 0
}

function Resolve-PythonW {
    # 1. 若存在 .venv，优先读取 pyvenv.cfg 里的 base home（避免 uv 转发器弹黑窗）
    $cfg = Join-Path $root '.venv\pyvenv.cfg'
    if (Test-Path $cfg) {
        $homeLine = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($homeLine) {
            $homeDir = ($homeLine -split '=', 2)[1].Trim()
            $cand = Join-Path $homeDir 'pythonw.exe'
            if (Test-Path $cand) { return $cand }
        }
    }
    # 2. 系统 PATH 里的 pythonw.exe
    $cmdW = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmdW -and (Test-Path $cmdW.Source)) { return $cmdW.Source }
    # 3. 与 python.exe 同目录的 pythonw.exe
    $cmdP = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmdP) {
        $sib = Join-Path (Split-Path $cmdP.Source -Parent) 'pythonw.exe'
        if (Test-Path $sib) { return $sib }
    }
    throw '未找到 pythonw.exe，请先安装 Python 3.10+ 并勾选加入 PATH。'
}

$pythonw = Resolve-PythonW
Write-Host "[检测] 使用无窗口解释器: $pythonw" -ForegroundColor Cyan

# 任务 1：开机自启轻量网站服务器（按需实时模式：无人打开网页时 0 网络请求、近 0 CPU）
$webAction = New-ScheduledTaskAction -Execute $pythonw -Argument 'run_server.pyw' -WorkingDirectory $root
$webTrigger = New-ScheduledTaskTrigger -AtLogOn
$webTrigger.Delay = 'PT30S'
$webSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'ETFRotationWeb' -Action $webAction -Trigger $webTrigger -Settings $webSettings -Force | Out-Null

# 任务 2：工作日收盘后 15:10 自动更新日线并导出 data.json（约 5 秒运行完即退出）
$updateAction = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$root\定时更新.bat`"" -WorkingDirectory $root
$updateTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '15:10'
$updateSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'ETFRotationDataUpdate' -Action $updateAction -Trigger $updateTrigger -Settings $updateSettings -Force | Out-Null

Write-Host "[成功] 已注册开机自启与收盘定时更新任务：" -ForegroundColor Green
Get-ScheduledTask -TaskName $taskNames | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime, LastTaskResult | Format-Table -AutoSize
