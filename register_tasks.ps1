$ErrorActionPreference = 'Stop'
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Set-ScheduledTask -TaskName 'ETFRotationWeb' -Settings $settings | Out-Null
Start-ScheduledTask -TaskName 'ETFRotationWeb'
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName 'ETFRotationWeb' | Select-Object TaskName, State
