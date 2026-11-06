# Registers a Windows scheduled task that runs watch.py inside WSL every
# 10 minutes while you're logged in.
#
# Why Task Scheduler rather than cron inside WSL: WSL shuts its VM down a few
# seconds after the last terminal closes, so cron would silently stop.
# Task Scheduler starts WSL on demand for each run.
#
# Usage (from WSL):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w install-windows-task.ps1)"
# Remove with:
#   Unregister-ScheduledTask -TaskName 'Amazon Stock Watch' -Confirm:$false

param(
    [string]$Distro = 'Ubuntu',
    [string]$ScriptPath = '/home/anirudh/amazon-stock-watch/watch.py',
    [int]$IntervalMinutes = 10
)

$ErrorActionPreference = 'Stop'
$taskName = 'Amazon Stock Watch'
$user = "$env:USERDOMAIN\$env:USERNAME"  # tasks need the qualified name

# conhost --headless runs the console program without flashing a window.
# --jitter 90 spreads the checks out so they don't hit Amazon like clockwork.
$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless wsl.exe -d $Distro -- python3 $ScriptPath --jitter 90"

# Repeat indefinitely from now, and also run once at logon.
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user

# Laptop-friendly: run on battery, catch up after sleep, never stack runs.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# Interactive logon: runs in your session, which Windows toasts require.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $repeat, $logon -Settings $settings -Principal $principal `
    -Description 'Checks Amazon for the S25 Ultra Whitesilver 512GB variant.' -Force |
    Out-Null

Write-Host "Registered '$taskName' (every $IntervalMinutes min)."
