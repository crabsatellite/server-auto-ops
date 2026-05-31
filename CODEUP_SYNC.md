# CodeUp Mirror Sync

This repo can trigger Alibaba Cloud CodeUp repository mirror sync without a
browser login.

Required GitHub Actions secrets:

- `ALIBABA_ACCESS_KEY_ID`
- `ALIBABA_ACCESS_KEY_SECRET`
- `CODEUP_ORGANIZATION_ID`
- `CODEUP_MC_REPOSITORY_ID`

Optional secrets:

- `CODEUP_REPOSITORY_IDS` for multiple CodeUp repositories, comma-separated.
- `CODEUP_REMOTE_ACCOUNT` for the remote GitHub clone account.
- `CODEUP_REMOTE_TOKEN` for the remote GitHub clone token or PAT.

Manual run:

```powershell
$env:ALIBABA_ACCESS_KEY_ID = "<aliyun-ak>"
$env:ALIBABA_ACCESS_KEY_SECRET = "<aliyun-sk>"
$env:CODEUP_ORGANIZATION_ID = "<codeup-organization-id>"
$env:CODEUP_REPOSITORY_ID = "<codeup-repository-id>"
$env:CODEUP_REMOTE_ACCOUNT = "<github-username>"
$env:CODEUP_REMOTE_TOKEN = "<github-pat>"
python scripts\codeup_mirror_sync.py
```

GitHub Actions runs every 30 minutes and can also be triggered manually from
the `Sync CodeUp Mirror` workflow. The Minecraft server repo can also trigger it
by sending a `repository_dispatch` event named `sync-codeup`.

Current Chifanla values:

- Organization: `Chifanla`
- `CODEUP_ORGANIZATION_ID`: `65bb261da854e4c241ac5426`
- Minecraft server CodeUp repository ID: `6917787`
- Minecraft server CodeUp URL:
  `https://codeup.aliyun.com/65bb261da854e4c241ac5426/crabsatellite/minecraft_chifanla_server_1.21.1`
