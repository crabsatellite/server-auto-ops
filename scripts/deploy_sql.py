"""Deploy SQL scripts to VPS and run tests via ECS RunCommand."""
import base64
import re
import sys
from ecs_control import run_command_result, get_status

GIT = r'C:\git\cmd\git.exe'
REPO_PATH = r'C:\SDGO'


def _b64(value):
    return base64.b64encode(value.encode('utf-8')).decode('ascii')


def _validate_db(db):
    if not re.fullmatch(r'[A-Za-z0-9_]+', db):
        raise ValueError(f'invalid database name: {db!r}')


def _sql_runner_script(db, timeout, sql_file=None, query=None):
    _validate_db(db)
    conn_str = f"Server=localhost;Database={db};User Id=sa;Password=123456;"
    if sql_file:
        source = f'$sql = Get-Content -LiteralPath $sourcePath -Raw -Encoding UTF8'
        source_vars = f"$sourcePath = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(sql_file)}'))"
    elif query is not None:
        source = '$sql = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($queryB64))'
        source_vars = f"$queryB64 = '{_b64(query)}'"
    else:
        raise ValueError('sql_file or query is required')

    return rf'''
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
{source_vars}
$connStr = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(conn_str)}'))

function Write-ReaderRows($reader) {{
    while ($reader.HasRows) {{
        while ($reader.Read()) {{
            $row = ""
            for ($i=0; $i -lt $reader.FieldCount; $i++) {{
                if ($i -gt 0) {{ $row += "`t" }}
                $row += $reader.GetValue($i).ToString()
            }}
            Write-Output $row
        }}
        [void]$reader.NextResult()
    }}
}}

try {{
    {source}
    $batches = $sql -split '(?m)^\s*GO\s*$'
    $conn = New-Object System.Data.SqlClient.SqlConnection $connStr
    $handler = [System.Data.SqlClient.SqlInfoMessageEventHandler]{{ param($s,$e) Write-Output $e.Message }}
    $conn.add_InfoMessage($handler)
    $conn.Open()
    try {{
        foreach ($batch in $batches) {{
            $batch = $batch.Trim()
            if ($batch -eq "") {{ continue }}
            $cmd = $conn.CreateCommand()
            $cmd.CommandText = $batch
            $cmd.CommandTimeout = {timeout}
            $reader = $cmd.ExecuteReader()
            try {{
                Write-ReaderRows $reader
            }} finally {{
                $reader.Close()
            }}
        }}
    }} finally {{
        $conn.remove_InfoMessage($handler)
        $conn.Close()
    }}
}} catch {{
    Write-Output "SQL ERROR: $($_.Exception.Message)"
    exit 1
}}
'''


def _run_remote_or_fail(cmd, timeout=60, label='remote command'):
    result = run_command_result(cmd, timeout=timeout)
    if result is None:
        raise RuntimeError(f'{label} timed out waiting for ECS RunCommand result')
    output = (result['output'] or '').strip()
    exit_code = result.get('exit_code')
    exit_ok = exit_code in (0, None, '') or str(exit_code) == '0'
    if result['status'] != 'Success' or not exit_ok:
        details = output or result.get('error_info') or result.get('error_code') or 'no output'
        raise RuntimeError(f'{label} failed: {details}')
    return output


def run_sql(db, sql_file=None, query=None, timeout=60):
    """Run SQL on VPS via PowerShell .NET SqlClient."""
    cmd = _sql_runner_script(db, timeout, sql_file=sql_file, query=query)
    return _run_remote_or_fail(cmd, timeout=timeout, label=f'SQL on {db}')


def deploy():
    """Pull latest SQL scripts and install stored procedures."""
    print('=== DEPLOY ===')

    # Handle git conflicts: stash untracked then pull
    output = _run_remote_or_fail(rf'''
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
    print('Git pull:', output if output else 'OK')

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
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
