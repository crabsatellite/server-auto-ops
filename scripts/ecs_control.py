"""Alibaba Cloud ECS control - start/stop/status + idle detection."""
import os, sys, time, base64
from alibabacloud_ecs20140526.client import Client
from alibabacloud_ecs20140526 import models
from alibabacloud_tea_openapi.models import Config

INSTANCE_ID = os.environ.get('ECS_INSTANCE_ID', 'i-j6cdvdlwbxy9zybehw6i')
REGION = os.environ.get('ECS_REGION', 'cn-hongkong')
IDLE_HOURS = int(os.environ.get('IDLE_HOURS', '24'))

config = Config(
    access_key_id=os.environ['ALIBABA_ACCESS_KEY_ID'],
    access_key_secret=os.environ['ALIBABA_ACCESS_KEY_SECRET'],
    endpoint=f'ecs.{REGION}.aliyuncs.com'
)
client = Client(config)


def get_status():
    req = models.DescribeInstancesRequest(
        region_id=REGION,
        instance_ids=f'["{INSTANCE_ID}"]'
    )
    resp = client.describe_instances(req)
    for inst in resp.body.instances.instance:
        return inst.status
    return None


def start():
    status = get_status()
    if status == 'Running':
        print('already-running')
        return 'already-running'
    if status != 'Stopped':
        print(f'cannot-start:{status}')
        return f'cannot-start:{status}'
    req = models.StartInstanceRequest(instance_id=INSTANCE_ID)
    client.start_instance(req)
    print('starting')
    return 'starting'


def stop():
    status = get_status()
    if status != 'Running':
        print(f'not-running:{status}')
        return
    req = models.StopInstanceRequest(
        instance_id=INSTANCE_ID,
        stopped_mode='StopCharging'
    )
    client.stop_instance(req)
    print('stopped')


def run_command(cmd, timeout=30):
    req = models.RunCommandRequest(
        region_id=REGION,
        instance_id=[INSTANCE_ID],
        type='RunPowerShellScript',
        command_content=cmd,
        timeout=timeout
    )
    resp = client.run_command(req)
    inv_id = resp.body.invoke_id
    for _ in range(timeout):
        time.sleep(2)
        dreq = models.DescribeInvocationResultsRequest(
            region_id=REGION, invoke_id=inv_id
        )
        dresp = client.describe_invocation_results(dreq)
        results = dresp.body.invocation.invocation_results.invocation_result
        if results and results[0].invocation_status not in ('Running', 'Pending'):
            return base64.b64decode(results[0].output).decode('utf-8', errors='replace') if results[0].output else ''
    return None


def check_idle():
    """Check if server is idle. Returns True if should shut down."""
    status = get_status()
    if status != 'Running':
        print(f'status:{status}, skip')
        return False

    # Check WireGuard handshakes + game port connections
    output = run_command(r'''
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$idle = $true

# Check WireGuard peer handshakes
$wgOutput = & "C:\Program Files\WireGuard\wg.exe" show sdgo latest-handshakes 2>&1
if ($LASTEXITCODE -eq 0) {
    foreach ($line in $wgOutput -split "`n") {
        $parts = $line.Trim() -split "\s+"
        if ($parts.Count -ge 2 -and $parts[1] -match "^\d+$") {
            $lastHandshake = [long]$parts[1]
            $elapsed = $now - $lastHandshake
            Write-Host "peer:$($parts[0].Substring(0,8)):${elapsed}s"
            if ($lastHandshake -gt 0 -and $elapsed -lt IDLE_THRESHOLD) { $idle = $false }
        }
    }
} else { Write-Host "wg-error" }

# Check active game connections (SDGO 5001, MC 25565)
$sdgo = (netstat -an | Select-String "5001.*ESTABLISHED").Count
$mc = (netstat -an | Select-String "25565.*ESTABLISHED").Count
Write-Host "connections: sdgo=$sdgo mc=$mc"
if ($sdgo -gt 0 -or $mc -gt 0) { $idle = $false }

if ($idle) { Write-Host "IDLE" } else { Write-Host "ACTIVE" }
'''.replace('IDLE_THRESHOLD', str(IDLE_HOURS * 3600)))

    if output is None:
        print('command-timeout, skip')
        return False

    print(output.strip())
    return 'IDLE' in output


def backup():
    """Run backup script on VPS before shutdown."""
    status = get_status()
    if status != 'Running':
        print(f'not running, skip backup')
        return
    print('Running backup...')
    output = run_command(r'''
$env:PATH += ";C:\git\cmd"
$env:GIT_TERMINAL_PROMPT = "0"

function Backup-Repo($path, $name) {
    if (-not (Test-Path "$path\.git")) { Write-Host "$name : no repo"; return }
    Set-Location $path
    $status = C:\git\cmd\git.exe status --porcelain 2>&1
    if (-not $status) { Write-Host "$name : clean"; return }
    $date = Get-Date -Format "yyyy/MM/dd HH:mm"
    C:\git\cmd\git.exe add -A 2>&1
    C:\git\cmd\git.exe commit -m "auto backup: $date" 2>&1
    C:\git\cmd\git.exe push origin HEAD 2>&1
    Write-Host "$name : backed up"
}

Backup-Repo "D:\MC" "Minecraft"
''', timeout=120)
    if output:
        print(output.strip())


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'start':
        start()
    elif cmd == 'stop':
        stop()
    elif cmd == 'status':
        print(get_status())
    elif cmd == 'check-idle':
        if check_idle():
            print('Idle threshold reached, backing up then stopping...')
            backup()
            stop()
        else:
            print('Server active or not running, no action.')
    elif cmd == 'backup':
        backup()
