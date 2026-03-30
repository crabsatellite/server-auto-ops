"""Deploy SQL scripts to VPS and run tests via ECS RunCommand."""
import sys, time
from ecs_control import run_command, get_status

GIT = r'C:\git\cmd\git.exe'
REPO_PATH = r'C:\SDGO'


def find_sqlcmd():
    """Find sqlcmd on VPS."""
    output = run_command(r'''
$paths = @(
    "C:\SDGO\LiteSQL\SQLCMD.EXE",
    "C:\SDGO\LiteSQL\Tools\Binn\SQLCMD.EXE",
    "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\SQLCMD.EXE",
    "C:\Program Files\Microsoft SQL Server\110\Tools\Binn\SQLCMD.EXE"
)
foreach ($p in $paths) { if (Test-Path $p) { Write-Host $p; return } }
# Search
$found = Get-ChildItem C:\SDGO -Recurse -Filter "SQLCMD.EXE" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($found) { Write-Host $found.FullName } else { Write-Host "NOT_FOUND" }
''', timeout=30)
    path = output.strip() if output else None
    if path and path != 'NOT_FOUND':
        return path
    return None


def run_sql(db, sql_file=None, query=None, timeout=60):
    """Run SQL on VPS. Returns output."""
    sqlcmd_path = getattr(run_sql, '_path', None)
    if not sqlcmd_path:
        print('Finding sqlcmd...')
        sqlcmd_path = find_sqlcmd()
        if not sqlcmd_path:
            print('ERROR: sqlcmd not found on VPS')
            sys.exit(1)
        print(f'Found: {sqlcmd_path}')
        run_sql._path = sqlcmd_path

    if sql_file:
        cmd = f'& "{sqlcmd_path}" -S localhost -U sa -P 123456 -d {db} -i "{sql_file}" 2>&1'
    else:
        cmd = f'& "{sqlcmd_path}" -S localhost -U sa -P 123456 -d {db} -Q "{query}" 2>&1'
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
