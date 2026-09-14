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
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; inspect it before replacing it.'
}
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# pythonw has no console window. The listener writes diagnostic logs itself.
$pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is required for a hidden listener.' }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}" listen --config "{1}"' -f $script,$Config) -WorkingDirectory (Split-Path $script)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Poll trusted GitHub Minecraft signals; never shut down the host.' -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
} catch {
    # Current-user logon fallback does not require elevation or stored passwords.
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $valueName = 'MinecraftLocalWakeListener'
    $existing = (Get-ItemProperty -LiteralPath $key).PSObject.Properties[$valueName].Value
    $command = '"{0}" "{1}" listen --config "{2}"' -f $pythonw,$script,$Config
    if ($existing -and $existing -ne $command) { throw 'A different startup command exists; inspect before replacing.' }
    New-ItemProperty -LiteralPath $key -Name $valueName -Value $command -PropertyType String -Force -ErrorAction Stop | Out-Null
    Start-Process -FilePath $pythonw -ArgumentList ('"{0}" listen --config "{1}"' -f $script,$Config) -WindowStyle Hidden
    Write-Output 'Listener installed in current-user Run key and started hidden (scheduled task unavailable).'
}
