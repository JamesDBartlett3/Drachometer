#Requires -Version 5
<#
What is this script for?
This script installs a background helper that checks in every minute; if 5+ hours have passed since the last ping (or it has never pinged), it pings the Claude Code CLI and records the new timestamp to a small state file. It starts at logon and on screen unlock. Task Scheduler is always allowed to start a new instance, even if it believes an older one is still running (MultipleInstancesPolicy=Parallel), so a stale "still running" flag can never block it. On startup, each instance kills any older copies of itself (matched by command line via WMI) so at most one loop is doing the work at a time; that cleanup is a courtesy, not a requirement -- the state file makes every check idempotent, so a brief overlap between an old and new instance is harmless either way.

Why is this useful?
Claude usage is limited to a certain amount of tokens per 5-hour window, but that window only starts counting when you send your first prompt after the previous 5-hour window expires. Consequently, the time between logging on and sending your first prompt for the day is time you're not accumulating toward a fresh window. Similarly, any time between the end of a 5-hour window and the start of your next prompt is time you're not accumulating toward your next 5-hour window. This script ensures that you are always accumulating toward a fresh window, regardless of the time of day when you send your first prompt, and regardless of any delay between the end of one 5-hour window and the start of your next prompt. By checking every minute and pinging as soon as the state file shows a window has expired, the clock stays fresh across the day to within a minute -- whether or not you ever lock your screen -- resuming from wherever it left off, even after sleep, hibernation, or a missed check.
#>

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$destDir = 'C:\tools\startup'
$vbsPath = Join-Path $destDir 'Claude Runner 2049.vbs'
$statePath = Join-Path $destDir 'claude-runner-last-ping.txt'
$xmlPath = Join-Path $env:TEMP 'Claude Runner 2049.xml'
$taskName = 'Claude Runner 2049'

New-Item -ItemType Directory -Force -Path $destDir | Out-Null

@"
Dim objShell, objFSO, stateFile, command, startTime

Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
stateFile = "$statePath"
command = "cmd /c start /b cmd.exe /c claude --safe-mode --no-session-persistence --disable-slash-commands --effort low --model haiku -p hi"

KillOlderInstances()
startTime = Now()

' Checks every minute, for up to 24 hours per instance -- just a leak valve in case
' KillOlderInstances ever fails silently (e.g. a permissions quirk); correctness
' comes from the state file being checked idempotently, not from this instance
' living that long.
Do While DateDiff("s", startTime, Now()) < 86400
    CheckAndPing()
    WScript.Sleep(60000)
Loop

' Any other wscript.exe running this same script is an older instance. PIDs get
' recycled by Windows so "highest PID" is not reliably "newest" -- CreationDate is,
' so the most-recently-created matching process is this one; every other match
' gets terminated.
Sub KillOlderInstances()
    Dim scriptPath, wmi, procs, p, myCreated
    scriptPath = WScript.ScriptFullName
    Set wmi = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
    Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine, CreationDate FROM Win32_Process WHERE Name='wscript.exe'")

    myCreated = ""
    For Each p In procs
        If InStr(1, p.CommandLine, scriptPath, 1) > 0 Then
            If p.CreationDate > myCreated Then myCreated = p.CreationDate
        End If
    Next

    For Each p In procs
        If InStr(1, p.CommandLine, scriptPath, 1) > 0 And p.CreationDate <> myCreated Then
            p.Terminate()
        End If
    Next
End Sub

Sub CheckAndPing()
    Dim nowTime, elapsedSeconds, lastPing, f
    nowTime = Now()
    elapsedSeconds = 18001 ' no state file yet -> always ping

    If objFSO.FileExists(stateFile) Then
        Set f = objFSO.OpenTextFile(stateFile, 1)
        lastPing = f.ReadLine()
        f.Close
        If IsDate(lastPing) Then elapsedSeconds = DateDiff("s", CDate(lastPing), nowTime)
    End If

    If elapsedSeconds >= 18001 Then
        Set f = objFSO.CreateTextFile(stateFile, True)
        f.WriteLine CStr(nowTime)
        f.Close
        objShell.Run command, 0, False
    End If
End Sub
"@ | Set-Content -Path $vbsPath -Encoding ASCII

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
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
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
Write-Host "State file: $statePath"
Write-Host "Scheduled task '$taskName' registered (starts at logon and on session unlock; checks every minute for up to 24h per instance)."
