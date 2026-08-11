$ErrorActionPreference = 'Stop'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '15:10'
Set-ScheduledTask -TaskName 'ETFRotationDataUpdate' -Trigger $trigger | Out-Null
Get-ScheduledTaskInfo -TaskName 'ETFRotationDataUpdate' | Select-Object NextRunTime
