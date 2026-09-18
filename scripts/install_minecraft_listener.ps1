[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$TaskName = 'Minecraft Local Wake Listener'
)
$ErrorActionPreference = 'Stop'
$Config = (Resolve-Path -LiteralPath $Config).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
$script = Join-Path $PSScriptRoot 'minecraft_listener.py'
$pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is required for a hidden listener.' }
$argsText = '"{0}" supervise --config "{1}"' -f $script,$Config
$command = '"{0}" {1}' -f $pythonw,$argsText
$oldCommand = '"{0}" "{1}" listen --config "{2}"' -f $pythonw,$script,$Config
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$valueName = 'MinecraftLocalWakeListener'
$existingRun = (Get-ItemProperty -LiteralPath $key).PSObject.Properties[$valueName].Value
if ($existingRun -and $existingRun -notin @($oldCommand,$command)) { throw 'Unrecognized startup command; refusing replacement.' }
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    if (@($existingTask.Actions).Count -ne 1 -or $existingTask.Actions[0].Execute -ne $pythonw -or $existingTask.Actions[0].Arguments -notin @($argsText,('"{0}" listen --config "{1}"' -f $script,$Config))) {
        throw 'Task name belongs to a different command; inspect before replacing.'
    }
    if ($existingTask.State -eq 'Running') { throw 'Existing task is running; inspect its owned polling processes before upgrading.' }
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $argsText -WorkingDirectory $PSScriptRoot
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $user),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
)
$registered=$false
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Supervise trusted Minecraft wake polling; never shut down the host.' -Force -ErrorAction Stop | Out-Null
    $registered=$true
} catch {
    Write-Warning ('Scheduled task unavailable ({0}); using current-user logon fallback.' -f $_.Exception.GetType().Name)
}
if ($registered) {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($existingRun) { Remove-ItemProperty -LiteralPath $key -Name $valueName -ErrorAction Stop }
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
} else {
    # No elevation or stored password. Both routes point to the same singleton supervisor.
    New-ItemProperty -LiteralPath $key -Name $valueName -Value $command -PropertyType String -Force -ErrorAction Stop | Out-Null
    $startup = [Environment]::GetFolderPath('Startup')
    if (-not (Test-Path -LiteralPath $startup)) { throw 'Existing Startup folder unavailable.' }
    $linkPath=Join-Path $startup 'Minecraft Local Wake Supervisor.lnk'
    $ws=New-Object -ComObject WScript.Shell
    $link=$ws.CreateShortcut($linkPath)
    if ((Test-Path -LiteralPath $linkPath) -and ($link.TargetPath -ne $pythonw -or $link.Arguments -ne $argsText)) {
        throw 'Unrecognized startup shortcut; refusing replacement.'
    }
    $link.TargetPath=$pythonw; $link.Arguments=$argsText; $link.WorkingDirectory=$PSScriptRoot; $link.WindowStyle=7
    $link.Description='Supervised Minecraft wake listener (no host shutdown)'; $link.Save()
    Start-Process -FilePath $pythonw -ArgumentList $argsText -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    Write-Output 'Installed singleton supervisor via current-user Run + Startup shortcut; login and awake PC required.'
}
