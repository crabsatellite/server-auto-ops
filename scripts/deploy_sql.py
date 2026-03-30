"""Deploy SQL scripts to VPS and run tests via ECS RunCommand."""
import sys, time
from ecs_control import run_command, get_status

GIT = r'C:\git\cmd\git.exe'
REPO_PATH = r'C:\SDGO'


def run_sql(db, sql_file=None, query=None, timeout=60):
    """Run SQL on VPS via PowerShell .NET SqlClient."""
    if sql_file:
        # Read file and execute
        cmd = rf'''
$sql = Get-Content "{sql_file}" -Raw -Encoding UTF8
# Split on GO statements for batch execution
$batches = $sql -split '(?m)^\s*GO\s*$'
$conn = New-Object System.Data.SqlClient.SqlConnection "Server=localhost;Database={db};User Id=sa;Password=123456;"
$conn.Open()
foreach ($batch in $batches) {{
    $batch = $batch.Trim()
    if ($batch -eq "") {{ continue }}
    try {{
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $batch
        $cmd.CommandTimeout = {timeout}
        # Capture messages
        $handler = [System.Data.SqlClient.SqlInfoMessageEventHandler]{{ param($s,$e) Write-Host $e.Message }}
        $conn.add_InfoMessage($handler)
        $reader = $cmd.ExecuteReader()
        while ($reader.HasRows) {{
            while ($reader.Read()) {{
                $row = ""
                for ($i=0; $i -lt $reader.FieldCount; $i++) {{
                    if ($i -gt 0) {{ $row += "`t" }}
                    $row += $reader.GetValue($i).ToString()
                }}
                Write-Host $row
            }}
            [void]$reader.NextResult()
        }}
        $reader.Close()
        $conn.remove_InfoMessage($handler)
    }} catch {{
        Write-Host "SQL ERROR: $($_.Exception.Message)"
    }}
}}
$conn.Close()
'''
    else:
        cmd = rf'''
$conn = New-Object System.Data.SqlClient.SqlConnection "Server=localhost;Database={db};User Id=sa;Password=123456;"
$conn.Open()
$handler = [System.Data.SqlClient.SqlInfoMessageEventHandler]{{ param($s,$e) Write-Host $e.Message }}
$conn.add_InfoMessage($handler)
$cmd = $conn.CreateCommand()
$cmd.CommandText = "{query}"
$cmd.CommandTimeout = {timeout}
$reader = $cmd.ExecuteReader()
while ($reader.HasRows) {{
    while ($reader.Read()) {{
        $row = ""
        for ($i=0; $i -lt $reader.FieldCount; $i++) {{
            if ($i -gt 0) {{ $row += "`t" }}
            $row += $reader.GetValue($i).ToString()
        }}
        Write-Host $row
    }}
    [void]$reader.NextResult()
}}
$reader.Close()
$conn.Close()
'''
    output = run_command(cmd, timeout=timeout)
    return output.strip() if output else 'TIMEOUT'


def deploy():
    """Pull latest SQL scripts and install stored procedures."""
    print('=== DEPLOY ===')

    # Handle git conflicts: stash untracked then pull
    output = run_command(rf'''
$env:GIT_TERMINAL_PROMPT = "0"
Set-Location {REPO_PATH}
# Move conflicting files if they exist
foreach ($f in @("start_all.ps1", "update_db.sql")) {{
    if (Test-Path $f) {{ Rename-Item $f "$f.bak" -Force -ErrorAction SilentlyContinue }}
}}
& {GIT} pull origin main 2>&1
# Restore backups if pull created the files
foreach ($f in @("start_all.ps1", "update_db.sql")) {{
    if (Test-Path "$f.bak") {{
        if (Test-Path $f) {{ Remove-Item "$f.bak" }} else {{ Rename-Item "$f.bak" $f }}
    }}
}}
''', timeout=60)
    print('Git pull:', output.strip() if output else 'TIMEOUT')

    # Install sp_Daily_SortAndDedup
    print('\nInstalling sp_Daily_SortAndDedup...')
    result = run_sql('GOnlineGame', sql_file=rf'{REPO_PATH}\sql_scripts\GOnlineGame\sp_Daily_SortAndDedup.sql')
    print(result)

    # Install mail command system
    print('\nInstalling mail command system...')
    result = run_sql('GDCommon', sql_file=rf'{REPO_PATH}\sql_scripts\sp_MailCommand_Setup.sql')
    print(result)


def test():
    """Run test suite on VPS."""
    print('\n=== TEST ===')
    output = run_sql('GOnlineGame',
                     sql_file=rf'{REPO_PATH}\sql_scripts\GOnlineGame\test_sp_Daily_SortAndDedup.sql',
                     timeout=120)
    print(output)

    # Check for failures
    lines = output.split('\n')
    passes = sum(1 for l in lines if 'PASS' in l)
    fails = sum(1 for l in lines if 'FAIL' in l)
    skips = sum(1 for l in lines if 'SKIP' in l)

    print(f'\n=== RESULTS: {passes} passed, {fails} failed, {skips} skipped ===')
    if fails > 0:
        sys.exit(1)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'deploy-and-test'

    status = get_status()
    if status != 'Running':
        print(f'VPS is {status}, cannot deploy')
        sys.exit(1)

    if action in ('deploy', 'deploy-and-test'):
        deploy()
    if action in ('test', 'deploy-and-test'):
        test()


if __name__ == '__main__':
    main()
