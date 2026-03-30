"""Deploy SQL scripts to VPS and run tests via ECS RunCommand."""
import sys, time
from ecs_control import run_command, get_status

SQLCMD = r'C:\SDGO\LiteSQL\SQLCMD.EXE -S localhost -U sa -P 123456'
GIT = r'C:\git\cmd\git.exe'
REPO_PATH = r'C:\SDGO'


def deploy():
    """Pull latest SQL scripts and install stored procedures."""
    print('=== DEPLOY ===')

    # Pull latest from SDGO-server repo
    output = run_command(rf'''
$env:GIT_TERMINAL_PROMPT = "0"
Set-Location {REPO_PATH}
& {GIT} pull origin main 2>&1
''', timeout=60)
    print('Git pull:', output.strip() if output else 'TIMEOUT')

    # Install sp_Daily_SortAndDedup
    print('\nInstalling sp_Daily_SortAndDedup...')
    output = run_command(rf'''
{SQLCMD} -d GOnlineGame -i "{REPO_PATH}\sql_scripts\GOnlineGame\sp_Daily_SortAndDedup.sql" 2>&1
''', timeout=60)
    print(output.strip() if output else 'TIMEOUT')

    # Install mail command system
    print('\nInstalling mail command system...')
    output = run_command(rf'''
{SQLCMD} -d GDCommon -i "{REPO_PATH}\sql_scripts\sp_MailCommand_Setup.sql" 2>&1
''', timeout=60)
    print(output.strip() if output else 'TIMEOUT')


def test():
    """Run test suite on VPS."""
    print('=== TEST ===')
    output = run_command(rf'''
{SQLCMD} -d GOnlineGame -i "{REPO_PATH}\sql_scripts\GOnlineGame\test_sp_Daily_SortAndDedup.sql" 2>&1
''', timeout=120)
    if output is None:
        print('TIMEOUT: Test execution timed out')
        sys.exit(1)

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
