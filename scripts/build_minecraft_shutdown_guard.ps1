[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$JavaHome)
$ErrorActionPreference = 'Stop'
$javac = Join-Path $JavaHome 'bin\javac.exe'
$jar = Join-Path $JavaHome 'bin\jar.exe'
& $javac --release 21 --add-modules jdk.attach (Join-Path $PSScriptRoot 'MinecraftShutdownGuard.java')
if ($LASTEXITCODE -ne 0) { throw 'Java compilation failed.' }
& $jar --create --file (Join-Path $PSScriptRoot 'minecraft-shutdown-guard.jar') --manifest (Join-Path $PSScriptRoot 'minecraft-shutdown-guard.mf') -C $PSScriptRoot MinecraftShutdownGuard.class
if ($LASTEXITCODE -ne 0) { throw 'Jar packaging failed.' }
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot 'minecraft-shutdown-guard.jar')
