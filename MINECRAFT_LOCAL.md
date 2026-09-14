# Minecraft local wake controller

Minecraft-only. The legacy `start-server.yml` ECS/SDGO workflow is intentionally unchanged.

## Operation

1. Preserve and verify the stopped canonical server checkout (including ignored files) with `scripts/backup_minecraft_local.py`. Preserve new remote Git history before a one-time reviewed fast-forward. Cloud disks are a separate source; a local backup does not certify their contents.
2. Adapt the existing server sync script to support `-LocalAuthoritative`: never import a newer remote world automatically after cutover; never force push or merge region files.
3. Keep a private JSON config **outside this public repository**. Fields required by `minecraft_listener.py`: repository, branch, workflow, allowed_actors, server_root, java, java_args, gh, state_prefix, backup_parent, port. Optional defaults: poll_seconds=30, request_ttl_seconds=600, health_seconds=10, idle_seconds=1800. Use absolute local paths. Credentials come from the current user's existing `gh auth login`, never from workflow payloads.
4. Run `scripts/install_minecraft_listener.ps1 -Config <private-config> -Python <python.exe>` under that user. It installs a hidden interactive-logon scheduled task and starts it now; the PC must remain awake and the user logged in. It does not change power settings or start at pre-login boot.
5. Run **Start Minecraft (local host)** in GitHub Actions, or `python scripts/minecraft_listener.py request --config <private-config>`. The repository_dispatch type is **start-minecraft**, NOT the old start-server. Existing callers must select this new event; the legacy event still targets ECS.
6. `... status --config ...` reads local listener/runtime evidence. `... stop --config ...` requests save-all flush + stop, never OS shutdown.

## Safety and lifecycle

- Only successful completed runs of the exact workflow, repository, branch and allowed actor are accepted, created after activation and within 10 minutes. A consumed run never replays. A request during active startup/play/backup does not start another JVM.
- A green workflow is only a wake signal. Java protocol readiness, world persistence, and actual remote-player connectivity are separate tests.
- Git synchronization is fail-closed. GitHub downtime may prevent starting; failed post-stop pushes leave local commits/data in place. Inspect `.sync.log` and reconcile before retrying.
- An empty server stops after 30 minutes. Failed status probes are not evidence of an empty server. Never force-kill Java to meet a shutdown deadline.
- After shutdown, ignored authoritative files are archived and hash-verified, then tracked state is committed/pushed with remote SHA verification. Initial full archive + Git bundles are the migration rollback copy; don't delete them. Session archives have no automatic retention deletion.
- Process locks prevent duplicate workers. A live orphan process blocks new startup and requires inspection. No arbitrary remote commands or downloaded scripts are executed.
- Keep the Minecraft listener bound to loopback until ingress/authentication is deliberately configured. Offline-mode without authentication is not safe for unguarded public forwarding. Do not change online-mode blindly: player UUID/inventory mapping can change.
- SDGO, cloud lifecycle, billing, public relay setup and cloud-exclusive final data recovery are outside this controller.

## Tests

`python -m unittest discover -s scripts -p test_minecraft_listener.py -v`

End-to-end acceptance: activate listener, dispatch new workflow, verify local JVM ready via protocol/logs, gracefully stop, verify backup/remote SHA, repeat with another workflow run. Verify actual external client gameplay separately before claiming Internet access works.
