# Auto backup: commit and push game data changes
# Used by: scheduled task at noon + before shutdown
$env:PATH += ";C:\git\cmd;C:\java\bin"

function Backup-Repo($path, $name) {
    if (-not (Test-Path "$path\.git")) {
        Write-Host "$name : not a git repo, skip"
        return
    }
    Set-Location $path
    $status = C:\git\cmd\git.exe status --porcelain 2>&1
    if (-not $status) {
        Write-Host "$name : no changes"
        return
    }
    $date = Get-Date -Format "yyyy/MM/dd HH:mm"
    C:\git\cmd\git.exe add -A
    C:\git\cmd\git.exe commit -m "auto backup: $date"
    C:\git\cmd\git.exe push origin HEAD 2>&1
    Write-Host "$name : backed up at $date"
}

# MC server
Backup-Repo "D:\MC" "Minecraft"

Write-Host "Backup complete"
