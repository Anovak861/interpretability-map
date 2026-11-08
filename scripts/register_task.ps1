# Registers (or replaces) a Windows scheduled task that runs scripts\run_update.ps1.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Every Daily -At 03:00
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Every Weekly -DayOfWeek Sunday -At 03:00
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
#
# The task runs only while you are logged in (headless CLI workers use your login), and
# StartWhenAvailable makes a run missed during sleep happen at the next opportunity.
param(
    [ValidateSet("Daily", "Weekly")] [string] $Every = "Weekly",
    [string] $At = "03:00",
    [System.DayOfWeek] $DayOfWeek = "Sunday",
    [switch] $Remove
)
$name = "InterpretabilityMapUpdate"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    "Removed scheduled task $name"
    return
}

$script = Join-Path $PSScriptRoot "run_update.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
if ($Every -eq "Daily") {
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
} else {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Interpretability Map: discover, triage and extract new documents" -Force |
    Out-Null
"Registered $name ($Every at $At). Logs: data\logs\scheduler.log and data\logs\update-*.md"
