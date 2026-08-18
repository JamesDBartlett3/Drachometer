#Requires -Version 5
<#
What is this script for?
This script installs a background helper that pings the Claude Code CLI when you logon/unlock in the morning, and then every 5 hours after that until midnight, or until you shut off your computer for the day, whichever comes first.

Why is this useful?
Claude usage is limited to a certain amount of tokens per 5-hour window, but that window only starts counting when you send your first prompt after the previous 5-hour window expires. Consequently, the time between logging on and sending your first prompt for the day is time you're not accumulating toward a fresh window. Similarly, any time between the end of a 5-hour window and the start of your next prompt is time you're not accumulating toward your next 5-hour window. This script ensures that you are always accumulating toward a fresh window, regardless of the time of day when you send your first prompt, and regardless of any delay between the end of one 5-hour window and the start of your next prompt. By pinging immediately at logon and again the instant each 5-hour window expires, the clock is always running in the background, maximizing how many full 5-hour windows fall within your normal working day.
#>

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$destDir = 'C:\tools\startup'
$vbsPath = Join-Path $destDir 'Claude Runner 2049.vbs'
$xmlPath = Join-Path $env:TEMP 'Claude Runner 2049.xml'
$taskName = 'Claude Runner 2049'

New-Item -ItemType Directory -Force -Path $destDir | Out-Null

@'
Dim objShell, startDate, lastRunTime, command

Set objShell = CreateObject("WScript.Shell")
command = "cmd /c start /b cmd.exe /c claude --safe-mode --no-session-persistence --disable-slash-commands --effort low --model haiku -p hi"

startDate = Date()

Call RunCommand(command, objShell)
lastRunTime = Now()

' Runs the command every ~5 hours until the calendar date changes, then exits
Do While Date() = startDate
    If DateDiff("s", lastRunTime, Now()) >= 18001 Then
        Call RunCommand(command, objShell)
        lastRunTime = Now()
    End If
    WScript.Sleep(1000)
Loop

Sub RunCommand(cmd, shell)
    shell.Run cmd, 0, False
End Sub
'@ | Set-Content -Path $vbsPath -Encoding ASCII

$currentUserSid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$env:USERDOMAIN\$env:USERNAME</Author>
    <URI>\$taskName</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
    </SessionStateChangeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$currentUserSid</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>P1D</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>12</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"$vbsPath"</Arguments>
    </Exec>
  </Actions>
</Task>
"@ | Set-Content -Path $xmlPath -Encoding Unicode

schtasks /create /tn "$taskName" /xml "$xmlPath" /f | Out-Null

Remove-Item $xmlPath -Force

Write-Host "Installed: $vbsPath"
Write-Host "Scheduled task '$taskName' registered (runs at logon and on session unlock)."
