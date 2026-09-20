<#
.SYNOPSIS
  Register Curie's maintenance jobs as Windows scheduled tasks.

.DESCRIPTION
  Run from an elevated PowerShell in the sap_ai_chat directory:

      .\service\install-tasks.ps1                  # register the four tasks, triggers DISABLED
      .\service\install-tasks.ps1 -Enable          # register (or update) with the triggers enabled
      .\service\install-tasks.ps1 -Remove          # unregister the four tasks
      .\service\install-tasks.ps1 -Start review    # start one task now (review | consolidate | eval | housekeeping)

  The tasks and their triggers (only when -Enable was given):

      Curie review          curie --review-knowledge --apply     daily 02:00
      Curie consolidate     curie --consolidate                  weekly, Sunday 03:00
      Curie eval            curie --eval                         no trigger: by hand only
      Curie housekeeping    curie --housekeeping                 weekly, Saturday 04:00

  On a development host register them without -Enable and start them by
  hand (-Start, or Start-ScheduledTask "Curie review" in PowerShell, or
  the Task Scheduler window). Each task runs venv\Scripts\python.exe
  -m curie <options> in the project directory, so .env is found as usual;
  its console output goes to service\logs\task-<name>.log (overwritten
  each run; the jobs write their own reports next to the notes).

  The tasks run under the account that registers them, whether or not
  that account is logged on (S4U logon, no password stored). They need
  the same .env, SAP reachability and model endpoint as the service.
#>
[CmdletBinding()]
param(
    [switch]$Enable,
    [switch]$Remove,
    [ValidateSet("review", "consolidate", "eval", "housekeeping")]
    [string]$Start,
    [string]$Prefix = "Curie"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $PSScriptRoot "logs"

$jobs = @(
    @{ Name = "review";       Args = "--review-knowledge --apply"; Trigger = { New-ScheduledTaskTrigger -Daily -At 02:00 } },
    @{ Name = "consolidate";  Args = "--consolidate";              Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 03:00 } },
    @{ Name = "eval";         Args = "--eval";                     Trigger = $null },
    @{ Name = "housekeeping"; Args = "--housekeeping";             Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 04:00 } }
)

function TaskName($job) { "$Prefix $($job.Name)" }

if ($Start) {
    $name = "$Prefix $Start"
    Start-ScheduledTask -TaskName $name
    Write-Host "Started task '$name'. Output: $(Join-Path $logDir "task-$Start.log")"
    return
}

if ($Remove) {
    foreach ($job in $jobs) {
        $name = TaskName $job
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed task '$name'."
        }
    }
    return
}

if (-not (Test-Path $python)) { throw "venv not found: $python (run python -m venv venv first)" }
New-Item -ItemType Directory -Force $logDir | Out-Null

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -DontStopOnIdleEnd

foreach ($job in $jobs) {
    $name = TaskName $job
    $log = Join-Path $logDir "task-$($job.Name).log"
    # cmd.exe redirects the console output; python writes the job's own reports.
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"`"$python`" -m curie $($job.Args) > `"$log`" 2>&1`"" `
        -WorkingDirectory $root
    $params = @{ TaskName = $name; Action = $action; Principal = $principal; Settings = $settings
                 Description = "Curie maintenance: curie $($job.Args) in $root"; Force = $true }
    if ($job.Trigger -ne $null) {
        $trigger = & $job.Trigger
        $trigger.Enabled = [bool]$Enable
        $params.Trigger = $trigger
    }
    Register-ScheduledTask @params | Out-Null
    $state = if ($job.Trigger -eq $null) { "by hand only" } elseif ($Enable) { "trigger enabled" } else { "trigger disabled" }
    Write-Host ("Registered task '{0}' ({1}): curie {2}" -f $name, $state, $job.Args)
}
Write-Host ""
Write-Host "Start one now:  .\service\install-tasks.ps1 -Start review"
Write-Host "Enable triggers: .\service\install-tasks.ps1 -Enable"
