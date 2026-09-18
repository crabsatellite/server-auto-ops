# Minecraft local wake controller

Minecraft-only. Both `start-server` (legacy callers) and `start-minecraft` now signal the local PC. Neither start workflow starts Aliyun ECS or SDGO. Other cloud/SDGO workflows and resources are unchanged.

## Operation

1. Preserve and verify the stopped canonical server checkout (including ignored files) with `scripts/backup_minecraft_local.py`. Preserve new remote Git history before a one-time reviewed fast-forward. Cloud disks are a separate source; a local backup does not certify their contents.
2. Adapt the existing server sync script to support `-LocalAuthoritative`: never import a newer remote world automatically after cutover; never force push or merge region files.
3. Keep a private JSON config **outside this public repository**. Fields required by `minecraft_listener.py`: repository, branch, workflows, allowed_actors, server_root, java, java_args, gh, state_prefix, backup_parent, port. Optional defaults: poll_seconds=30, request_ttl_seconds=600, health_seconds=10, idle_seconds=1800. Set `workflows` to `["start-minecraft.yml", "start-server.yml"]` to accept both exact workflow paths; a legacy single `workflow` configuration is still supported. Use absolute local paths. Credentials come from the current user's existing `gh auth login`, never from workflow payloads.
4. Run `scripts/install_minecraft_listener.ps1 -Config <private-config> -Python <python.exe>` under that user. It installs a hidden supervisor: preferably a scheduled task with logon, recurring recovery and restart-on-failure; if task registration is denied, current-user Run plus a managed Startup shortcut point to the same singleton supervisor. The supervisor restarts only its polling child after exit or a 5-minute heartbeat stall, never a JVM or server worker. The PC must remain awake and the user logged in; the fallback is not a pre-login Windows service and cannot recover if every supervisor process is externally killed until logon/manual restart. It does not change power settings.
5. Run **Start Server** (original remote entry) or **Start Minecraft (local host)** in GitHub Actions. `python scripts/minecraft_listener.py request --event start-server --config <private-config>` verifies the original event; omitting `--event` uses start-minecraft. Both workflows publish local wake signals only, without cloud credentials or SDKs. Existing remote callers using start-server need no change.
6. `... status --config ...` reads supervisor, listener heartbeat and runtime evidence. `... stop --config ...` requests save-all flush + stop, never OS shutdown.

## Safety and lifecycle

- Only successful completed runs of the exact workflow, repository, branch and allowed actor are accepted, created after activation and within 10 minutes. A consumed run never replays. A request during active startup/play/backup does not start another JVM.
- A green workflow is only a wake signal. Java protocol readiness, world persistence, and actual remote-player connectivity are separate tests.
- Git synchronization is fail-closed. GitHub downtime may prevent starting; failed post-stop pushes leave local commits/data in place. Inspect `.sync.log` and reconcile before retrying.
- An empty server stops after 30 minutes. Failed status probes are not evidence of an empty server. Never force-kill Java to meet a shutdown deadline.
- After shutdown, ignored authoritative files are archived and hash-verified, then tracked state is committed/pushed with remote SHA verification. Initial full archive + Git bundles are the migration rollback copy; don't delete them. Session archives have no automatic retention deletion.
- Process locks prevent duplicate workers. A live orphan process blocks new startup and requires inspection. No arbitrary remote commands or downloaded scripts are executed.
- Keep the Minecraft listener bound to loopback until ingress/authentication is deliberately configured. Offline-mode without authentication is not safe for unguarded public forwarding. Do not change online-mode blindly: player UUID/inventory mapping can change.
- Friend ingress uses native Sakura FRP only: `auth_mode = server` grants the verified caller IP an eight-hour game-port lease. A separate loopback HTTPS wake listener accepts the PROXY-v2 source supplied by frpc, verifies the limited client capability, authorizes that IP through Sakura, and queues the existing GitHub Action. Keep the Sakura account token and TLS private key outside Git. No SSH relay or fallback is used.
- Publish `start_server.bat`, `PCL/minecraft-frp.crt`, and native PCL `VersionAdvanceRun`/`VersionAdvanceRunWait`/`VersionServerEnter` settings together; verify the CodeUp mirror. Friends use `frp-cup.com:33347`; the physical host update override uses loopback. The existing prelaunch script uses scoped certificate verification, never insecure TLS. Restart an already-open PCL after update so it reloads its settings; normal launch wakes and automatically joins, without editing multiplayer favorites.
- SDGO, cloud lifecycle, billing and cloud-exclusive final data recovery remain outside this controller.

## Tests

`python -m unittest discover -s scripts -p test_minecraft_listener.py -v`

End-to-end acceptance: activate supervisor, dispatch the legacy start-server event, verify local JVM ready via protocol/logs, gracefully stop, verify backup/remote SHA, repeat with another workflow run. Terminate only the positively identified polling child while a JVM is running and confirm that it respawns without disrupting the worker/JVM. Verify actual external client gameplay separately before claiming Internet access works.

## Known PFM shutdown workaround (opt-in)

Runtime inspection of Paladin Furniture 1.4.4 identified an idle `PFMProvider.lambda$createWriter$1` waiting on an empty writer queue after all dimensions saved, the Minecraft Server thread ended, and JVM main returned. It prevents normal JVM exit. The controller can use `shutdown_guard` pointing to the supplied JDK-21 `minecraft-shutdown-guard.jar`.

After a requested graceful stop has waited 60 seconds, the helper attaches to **that owned JVM only**. It refuses recovery if the Minecraft server thread is alive, main has not returned, any unknown non-daemon worker exists, the exact PFM writer stack is absent, or its single-thread executor has queued work. It rechecks the idle state, calls orderly executor shutdown and interrupts only the known empty PFM queue wait. PFM handles that interruption and returns normally. No JVM/host kill, System.exit, chunk edits or mod removal occur. Other hangs remain blocked for manual investigation.

Source: `scripts/MinecraftShutdownGuard.java`. Rebuild with `scripts/build_minecraft_shutdown_guard.ps1 -JavaHome <JDK21>`. `java --add-modules jdk.attach -jar <guard.jar> <owned-pid> audit` inspects without releasing a writer; `release` performs the guarded cleanup. Do not apply it to unrelated processes.
