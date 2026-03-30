"""One-off: check if db_backup.py is running and list backup files on VPS."""
import os, sys, time, base64
from alibabacloud_ecs20140526.client import Client
from alibabacloud_ecs20140526 import models
from alibabacloud_tea_openapi.models import Config

INSTANCE_ID = 'i-j6cdvdlwbxy9zybehw6i'
REGION = 'cn-hongkong'

config = Config(
    access_key_id=os.environ['ALIBABA_ACCESS_KEY_ID'],
    access_key_secret=os.environ['ALIBABA_ACCESS_KEY_SECRET'],
    endpoint=f'ecs.{REGION}.aliyuncs.com'
)
client = Client(config)

PS_SCRIPT = r'''
# Check if python is still exporting
$py = Get-Process -Name python* -ErrorAction SilentlyContinue
if ($py) { Write-Host "PYTHON STILL RUNNING: $($py.Id)" } else { Write-Host "PYTHON NOT RUNNING" }

# Check db_backup directory
if (Test-Path "C:\SDGO\db_backup") {
    $dirs = Get-ChildItem "C:\SDGO\db_backup" -Directory | Select-Object Name
    foreach ($d in $dirs) {
        $count = (Get-ChildItem "C:\SDGO\db_backup\$($d.Name)" -File).Count
        $size = [math]::Round((Get-ChildItem "C:\SDGO\db_backup\$($d.Name)" -File -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
        Write-Host "$($d.Name): $count files, ${size}MB"
    }
} else { Write-Host "No db_backup dir" }

# Check _meta.json
if (Test-Path "C:\SDGO\db_backup\_meta.json") {
    Get-Content "C:\SDGO\db_backup\_meta.json"
}
'''

print("Sending command to VPS...")
req = models.RunCommandRequest(
    region_id=REGION,
    instance_id=[INSTANCE_ID],
    type='RunPowerShellScript',
    command_content=PS_SCRIPT,
    timeout=60
)
resp = client.run_command(req)
inv_id = resp.body.invoke_id
print(f"Invoke ID: {inv_id}, polling for result...")

for i in range(30):
    time.sleep(2)
    dreq = models.DescribeInvocationResultsRequest(
        region_id=REGION, invoke_id=inv_id
    )
    dresp = client.describe_invocation_results(dreq)
    results = dresp.body.invocation.invocation_results.invocation_result
    if results and results[0].invocation_status not in ('Running', 'Pending'):
        output = base64.b64decode(results[0].output).decode('utf-8', errors='replace') if results[0].output else '(no output)'
        print(f"\n--- VPS Output (status: {results[0].invocation_status}) ---")
        print(output)
        print("--- End ---")
        sys.exit(0)
    print(f"  poll {i+1}: {results[0].invocation_status if results else 'waiting'}...")

print("Timed out waiting for command result.")
sys.exit(1)
