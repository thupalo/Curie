<#
.SYNOPSIS
  Install Curie as a Windows service with NSSM (https://nssm.cc).

.DESCRIPTION
  Run from an elevated PowerShell in the sap_ai_chat directory:

      .\service\install-service.ps1                 # install and start
      .\service\install-service.ps1 -Uninstall      # stop and remove
      .\service\install-service.ps1 -Nssm C:\Tools\nssm.exe

  The service runs "venv\Scripts\python.exe -m curie" with the project
  directory as working directory (CURIE_HOME), so .env is found as usual. Console
  output goes to service\logs\curie-stdout.log (rotated by NSSM); the
  application log file is CURIE_LOG_FILE from .env.

  A service has no desktop, which is fine: Chrome is started on the
  end-user PC by the SAP GUI, not on this host.

  Stopping the service sends Ctrl+C (NSSM "Console" method), which Curie
  handles like a normal shutdown: sessions are closed and the gateway
  registrations released. Give it up to RFC_DISPATCH_TIMEOUT + 5 seconds.
#>
[CmdletBinding()]
param(
    [string]$Name = "Curie",
    [string]$Nssm = "nssm.exe",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $PSScriptRoot "logs"

if (-not (Get-Command $Nssm -ErrorAction SilentlyContinue)) {
    throw "nssm.exe not found. Download it from https://nssm.cc and put it on PATH, or pass -Nssm <path>."
}

if ($Uninstall) {
    & $Nssm stop $Name confirm | Out-Null
    & $Nssm remove $Name confirm
    Write-Host "Service $Name removed."
    return
}

if (-not (Test-Path $python)) { throw "venv not found: $python (run python -m venv venv first)" }
if (-not (Test-Path (Join-Path $root ".env")) -and -not (Test-Path (Join-Path (Split-Path $root -Parent) ".env"))) {
    throw ".env not found in $root or its parent"
}
New-Item -ItemType Directory -Force $logDir | Out-Null

& $Nssm install $Name $python "-m curie"
& $Nssm set $Name AppDirectory $root
& $Nssm set $Name AppEnvironmentExtra "CURIE_HOME=$root"
& $Nssm set $Name DisplayName "Curie - LLM chat for SAP"
& $Nssm set $Name Description "Registered RFC server SAP_AI plus the web UI for the SAP LLM chat"
& $Nssm set $Name Start SERVICE_AUTO_START
& $Nssm set $Name AppStdout (Join-Path $logDir "curie-stdout.log")
& $Nssm set $Name AppStderr (Join-Path $logDir "curie-stdout.log")
& $Nssm set $Name AppRotateFiles 1
& $Nssm set $Name AppRotateBytes 5242880
& $Nssm set $Name AppStopMethodSkip 0
& $Nssm set $Name AppStopMethodConsole 15000
& $Nssm set $Name AppExit Default Restart
& $Nssm set $Name AppRestartDelay 5000
& $Nssm start $Name
Write-Host "Service $Name installed and started. Check: curie --selftest"
