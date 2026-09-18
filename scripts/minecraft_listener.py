"""Consume trusted GitHub wake runs; own Minecraft, never the host power state.
Only stdlib + the user's existing gh login are needed. Runtime files stay outside Git.
"""
import argparse, contextlib, ctypes, hashlib, json, logging, os, re, socket, struct
import subprocess, sys, threading, time, urllib.request, zipfile
from datetime import datetime, timezone
from pathlib import Path

HIDDEN = 0x08000000 if os.name == "nt" else 0
LOG = logging.getLogger("minecraft")

def utcnow():
    return datetime.now(timezone.utc)

def atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default

@contextlib.contextmanager
def lock(path):
    f = open(path, "a+b")
    f.seek(0)
    if not f.read(1):
        f.write(b"0"); f.flush()
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        f.close()

def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0: return False
    if os.name == "nt":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.OpenProcess.restype = ctypes.c_void_p
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x100000, False, pid)
        if not h: return False
        try: return k.WaitForSingleObject(h, 0) == 258
        finally: k.CloseHandle(h)
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False

def varint(n):
    out = bytearray()
    while True:
        v = n & 127; n >>= 7
        out.append(v | (128 if n else 0))
        if not n: return bytes(out)

def recv_exact(s, count):
    if count > 1024 * 1024: raise ValueError("Oversized server status response")
    out = bytearray()
    while len(out) < count:
        b = s.recv(count - len(out))
        if not b: raise ConnectionError("Truncated response")
        out.extend(b)
    return bytes(out)

def recv_varint(s):
    n = 0
    for i in range(5):
        b = recv_exact(s, 1)[0]; n |= (b & 127) << (7*i)
        if not b & 128: return n
    raise ValueError("Invalid VarInt")

def status(host="127.0.0.1", port=25565, timeout=3):
    host_bytes = host.encode("utf-8")
    handshake = b"\x00" + varint(767) + varint(len(host_bytes)) + host_bytes + struct.pack(">H", port) + b"\x01"
    with socket.create_connection((host, port), timeout) as s:
        s.settimeout(timeout)
        s.sendall(varint(len(handshake)) + handshake + b"\x01\x00")
        length = recv_varint(s)
        if not 0 < length <= 1024*1024: raise ValueError("Invalid response size")
        if recv_varint(s) != 0: raise ValueError("Not a status packet")
        d = json.loads(recv_exact(s, recv_varint(s)).decode("utf-8"))
        players = d.get("players", {}).get("online")
        if not isinstance(players, int) or players < 0: raise ValueError("Invalid player count")
        return d

def port_busy(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 1): return True
    except OSError: return False

def wake_workflows(c):
    values=c.get("workflows", [c.get("workflow", "start-minecraft.yml")])
    if not isinstance(values,list) or not values or len(values)>4:
        raise ValueError("Expected 1-4 explicit wake workflow filenames")
    if any(not isinstance(v,str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.ya?ml",v) for v in values):
        raise ValueError("Invalid wake workflow filename")
    return list(dict.fromkeys(values))

def eligible(run, config, activated, seen, now=None):
    now = now or utcnow()
    if str(run.get("id")) in seen: return False
    if run.get("conclusion") != "success" or run.get("status") != "completed": return False
    if run.get("event") not in ("workflow_dispatch", "repository_dispatch"): return False
    if run.get("head_branch") != config["branch"]: return False
    if run.get("path", "").split("@")[0] not in {".github/workflows/"+w for w in wake_workflows(config)}: return False
    if run.get("repository", {}).get("full_name") != config["repository"]: return False
    if run.get("actor", {}).get("login") not in config["allowed_actors"]: return False
    if run.get("triggering_actor",run.get("actor",{})).get("login") not in config["allowed_actors"]: return False
    try: created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError): return False
    if created.tzinfo is None: return False
    age = (now-created).total_seconds()
    return activated <= created and 0 <= age <= config.get("request_ttl_seconds", 600)

class GitHub:
    def __init__(self, gh): self.gh = gh
    def api(self, path, method="GET", data=None):
        p = subprocess.run([self.gh, "auth", "token"], capture_output=True, creationflags=HIDDEN, timeout=20, stdin=subprocess.DEVNULL)
        if p.returncode: raise RuntimeError("GitHub credential unavailable; run gh auth login as this user")
        token = p.stdout.decode().strip()
        req = urllib.request.Request("https://api.github.com/" + path.lstrip("/"),
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Authorization":"Bearer "+token, "Accept":"application/vnd.github+json",
                     "X-GitHub-Api-Version":"2022-11-28", "Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw=r.read()
        return json.loads(raw) if raw else None

def runtime_paths(c):
    base=Path(c["state_prefix"])
    return {k:Path(str(base)+"."+suffix) for k,suffix in {
        "listener":"listener.json", "runtime":"runtime.json", "control":"control.json",
        "listener_lock":"listener.lock", "worker_lock":"worker.lock", "log":"log",
        "server_log":"server.log", "supervisor":"supervisor.json",
        "supervisor_lock":"supervisor.lock", "diagnostic_log":"supervisor-child.log"}.items()}

def start_worker(c, config_file, request_id):
    paths=runtime_paths(c)
    prior=read_json(paths["runtime"], {})
    if pid_alive(prior.get("worker_pid")) or pid_alive(prior.get("java_pid")) or port_busy(c["port"]):
        LOG.info("Already active; request %s will not start a second server", request_id)
        return "already_active"
    with paths["log"].open("a",encoding="utf-8") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", "--config", str(config_file),
            "--request-id", str(request_id)], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            creationflags=HIDDEN, close_fds=True)
    return "worker_requested"

def poll_wake_runs(gh,c):
    runs={}; errors={}
    for workflow in wake_workflows(c):
        try:
            response=gh.api(f"repos/{c['repository']}/actions/workflows/{workflow}/runs?status=completed&per_page=20")
            for run in response.get("workflow_runs",[]): runs[str(run["id"])]=run
        except Exception as exc:
            errors[workflow]=type(exc).__name__
    return sorted(runs.values(),key=lambda r:r.get("created_at","")),errors

def listen(c, config_file):
    paths=runtime_paths(c); gh=GitHub(c["gh"])
    with lock(paths["listener_lock"]):
        state=read_json(paths["listener"],None)
        if state is None:
            state={"activated_utc":utcnow().isoformat(),"seen":[],"requests":[]}
        activated=datetime.fromisoformat(state["activated_utc"])
        state.update(listener_pid=os.getpid(),listener_started_utc=utcnow().isoformat(),workflows=wake_workflows(c))
        atomic(paths["listener"],state)
        LOG.info("Listener active pid=%s; trusted workflows=%s",os.getpid(),wake_workflows(c))
        while True:
            try:
                runs,errors=poll_wake_runs(gh,c)
                for run in runs:
                    if not eligible(run,c,activated,state["seen"]): continue
                    # Consume durably BEFORE spawning: crash means fail-closed, never replay.
                    rid=str(run["id"]); state["seen"]=(state["seen"]+[rid])[-200:]
                    state["requests"]=(state["requests"]+[{"id":rid,"workflow":run["path"],"accepted_utc":utcnow().isoformat()}])[-100:]
                    atomic(paths["listener"],state)
                    result=start_worker(c,config_file,rid)
                    state["requests"][-1]["result"]=result; atomic(paths["listener"],state)
                    LOG.info("Request %s: %s",rid,result)
                state["last_poll_utc"]=utcnow().isoformat()
                state["workflow_errors"]=errors
                if not errors: state["last_success_utc"]=state["last_poll_utc"]
                state.pop("error",None)
                if errors: LOG.warning("Workflow poll errors: %s",errors)
                atomic(paths["listener"],state)
            except Exception as exc:
                # Never log response bodies or credential-bearing command lines.
                LOG.warning("Poll failed (%s); leaving all data untouched",type(exc).__name__)
                state["error"]=type(exc).__name__; state["last_poll_utc"]=utcnow().isoformat()
                atomic(paths["listener"],state)
            time.sleep(c.get("poll_seconds",30))

def listener_stalled(state, child_pid, started_at, now, stale_seconds):
    heartbeat=started_at
    if state.get("listener_pid")==child_pid:
        try:
            parsed=datetime.fromisoformat(state["last_poll_utc"])
            if parsed.tzinfo is not None: heartbeat=max(heartbeat,parsed.timestamp())
        except (KeyError,ValueError,TypeError): pass
    return now-heartbeat > stale_seconds

def supervise(c,config_file):
    """Watch only our polling child. Never terminate a worker, JVM or host."""
    paths=runtime_paths(c)
    with lock(paths["supervisor_lock"]):
        previous=read_json(paths["supervisor"],{})
        # Fail closed rather than fight an orphaned polling process from an earlier supervisor.
        if pid_alive(previous.get("child_pid")):
            raise RuntimeError("Prior listener is still alive; inspect before taking ownership")
        state={"pid":os.getpid(),"started_utc":utcnow().isoformat(),"restart_count":0,"child_pid":None}
        while True:
            child=None; started=time.time()
            try:
                with paths["diagnostic_log"].open("a",encoding="utf-8") as out:
                    child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"listen","--config",str(config_file)],
                        stdin=subprocess.DEVNULL,stdout=out,stderr=out,creationflags=HIDDEN,close_fds=True)
                state.update(child_pid=child.pid,phase="running",child_started_utc=utcnow().isoformat())
                LOG.info("Supervisor started listener pid=%s",child.pid)
                while child.poll() is None:
                    state["heartbeat_utc"]=utcnow().isoformat();atomic(paths["supervisor"],state)
                    try: observed=read_json(paths["listener"],{})
                    except (OSError,ValueError): observed={}
                    stale=max(300,6*c.get("poll_seconds",30),60*len(wake_workflows(c))+c.get("poll_seconds",30))
                    if listener_stalled(observed,child.pid,started,time.time(),stale):
                        LOG.error("Polling child stalled; restarting only its owned pid=%s",child.pid)
                        child.terminate();child.wait(timeout=15)
                        break
                    time.sleep(c.get("supervisor_check_seconds",5))
                state.update(child_pid=None,last_exit_code=child.returncode,phase="restarting")
            except Exception as exc:
                state["last_error_type"]=type(exc).__name__
                LOG.error("Supervisor error type=%s",type(exc).__name__)
                # Do not spawn a competing listener if the owned one may still be alive.
                if child is not None and child.poll() is None:
                    atomic(paths["supervisor"],state)
                    raise
            state["restart_count"]+=1;atomic(paths["supervisor"],state)
            time.sleep(c.get("supervisor_restart_seconds",5))


def sync(c,phase,exit_code=0):
    script=Path(c["server_root"])/"scripts"/"sync_local_server.ps1"
    command=["powershell.exe","-NoLogo","-NoProfile","-ExecutionPolicy","Bypass","-File",str(script),
             "-Phase",phase,"-LocalAuthoritative"]
    if phase=="post": command += ["-ServerExitCode",str(exit_code)]
    p=subprocess.run(command,cwd=c["server_root"],capture_output=True,creationflags=HIDDEN,timeout=900)
    with Path(c["state_prefix"]+".sync.log").open("ab") as log:
        log.write(p.stdout); log.write(p.stderr)
    LOG.info("Git preservation phase %s exit=%s",phase,p.returncode)
    if p.returncode: raise RuntimeError("Git synchronization failed; local state preserved. Inspect Git before retrying.")

def backup_ignored(c,request_id):
    root=Path(c["server_root"])
    p=subprocess.run(["git","-C",str(root),"ls-files","-z","--others","--ignored","--exclude-standard"],capture_output=True,check=True,creationflags=HIDDEN)
    keep=[]
    for value in p.stdout.decode("utf-8").split("\0"):
        if not value: continue
        # Runtime logs/locks and decompressed archive caches have no authoritative gameplay state.
        if value.endswith("session.lock") or value.startswith(("logs/","crash-reports/","config/worldedit/.archive-unpack/")) or value.endswith(".log"): continue
        path=(root/value).resolve()
        if root.resolve() not in path.parents or path.is_symlink(): raise RuntimeError("Unsafe backup path")
        if path.is_file(): keep.append((value,path))
    stamp=utcnow().strftime("%Y%m%dT%H%M%SZ")
    target=Path(c["backup_parent"])/("minecraft-ignored-"+stamp+".zip")
    manifest=[]
    with zipfile.ZipFile(target,"x",compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for relative,path in keep:
            data=path.read_bytes(); z.writestr(relative,data)
            manifest.append({"path":relative,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()})
        z.writestr("PRESERVATION-MANIFEST.json",json.dumps(manifest))
    with zipfile.ZipFile(target) as z:
        for row in manifest:
            if hashlib.sha256(z.read(row["path"])).hexdigest()!=row["sha256"]: raise RuntimeError("Ignored-file backup verification failed")
    return str(target)

class IdleTimer:
    """Only a continuously observed empty period qualifies for automatic stop."""
    def __init__(self, seconds):
        self.seconds=seconds; self.empty_since=None
    def observe(self, players, now):
        if players is None or players > 0:
            self.empty_since=None
            return False
        if self.empty_since is None: self.empty_since=now
        return now-self.empty_since >= self.seconds

def worker(c,request_id):
    paths=runtime_paths(c)
    with lock(paths["worker_lock"]):
        previous=read_json(paths["runtime"],{})
        if port_busy(c["port"]) or pid_alive(previous.get("java_pid")):
            raise RuntimeError("Existing Minecraft process; refusing duplicate start")
        state={"request_id":request_id,"worker_pid":os.getpid(),"java_pid":None,"phase":"preflight",
               "started_utc":utcnow().isoformat(),"players":None,"idle_seconds":c.get("idle_seconds",1800)}
        atomic(paths["runtime"],state)
        proc=None; stop_sent=False; stopped_requested_at=None; last_guard=0
        try:
            sync(c,"pre")
            root=Path(c["server_root"])
            command=[c["java"],"@"+str(root/"user_jvm_args.txt"),"@"+str(root/c["java_args"]),"nogui"]
            proc=subprocess.Popen(command,cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding="utf-8",errors="replace",bufsize=1,creationflags=HIDDEN)
            state.update(java_pid=proc.pid,phase="starting"); atomic(paths["runtime"],state)
            def pump():
                with paths["server_log"].open("a",encoding="utf-8") as f:
                    for line in proc.stdout: f.write(line); f.flush()
            thread=threading.Thread(target=pump,daemon=True); thread.start()
            idle_timer=IdleTimer(c.get("idle_seconds",1800))
            while proc.poll() is None:
                control=read_json(paths["control"],{})
                requested_stop=control.get("request_id")==request_id and control.get("command")=="stop"
                try:
                    info=status(port=c["port"]); players=info["players"]["online"]
                    state.update(phase="stopping" if stop_sent else "ready",players=players,status_utc=utcnow().isoformat())
                    idle=idle_timer.observe(players,time.monotonic())
                except Exception:
                    # A failed status probe is NOT evidence of an empty server.
                    state.update(players=None); idle=idle_timer.observe(None,time.monotonic())
                if (requested_stop or idle) and not stop_sent:
                    proc.stdin.write("save-all flush\nstop\n");proc.stdin.flush();stop_sent=True;stopped_requested_at=time.monotonic()
                    state["phase"]="stopping"; state["stop_reason"]="requested" if requested_stop else "idle"
                    LOG.info("Requested graceful Minecraft-only stop (%s)",state["stop_reason"])
                if stop_sent and c.get("shutdown_guard") and time.monotonic()-stopped_requested_at >= 60 and time.monotonic()-last_guard >= 60:
                    last_guard=time.monotonic()
                    # Helper refuses any live Minecraft/server/main or unrecognized non-daemon thread.
                    # Only releases the known PFM idle writer after normal server shutdown.
                    guard=subprocess.run([c["java"],"--add-modules","jdk.attach","-jar",c["shutdown_guard"],str(proc.pid),"release"],
                        capture_output=True,creationflags=HIDDEN,timeout=20)
                    state["shutdown_guard_exit"]=guard.returncode
                    LOG.info("Post-stop PFM guard exit=%s",guard.returncode)
                atomic(paths["runtime"],state)
                time.sleep(c.get("health_seconds",10))
            thread.join(timeout=10)
            state.update(phase="backing_up",exit_code=proc.returncode,java_pid=None);atomic(paths["runtime"],state)
            state["ignored_backup"]=backup_ignored(c,request_id)
            sync(c,"post",proc.returncode)
            state.update(phase="stopped",backup_verified=True,stopped_utc=utcnow().isoformat())
        except Exception as exc:
            state.update(phase="error",error=type(exc).__name__+": "+str(exc),backup_verified=False)
            LOG.error("Worker failed: %s",str(exc))
            # Never terminate Java or Windows on error. Preserve the world and require inspection.
        finally:
            state["worker_pid"]=None
            atomic(paths["runtime"],state)

def main():
    a=argparse.ArgumentParser(); a.add_argument("mode",choices=["listen","supervise","worker","request","status","stop"])
    a.add_argument("--config",required=True);a.add_argument("--request-id",default="manual")
    a.add_argument("--event",choices=["start-server","start-minecraft"],default="start-minecraft")
    args=a.parse_args();config_file=Path(args.config).resolve();c=read_json(config_file)
    paths=runtime_paths(c)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(paths["log"],encoding="utf-8")])
    wake_workflows(c)
    if args.mode=="listen": listen(c,config_file)
    elif args.mode=="supervise": supervise(c,config_file)
    elif args.mode=="worker": worker(c,args.request_id)
    elif args.mode=="request":
        GitHub(c["gh"]).api(f"repos/{c['repository']}/dispatches",method="POST",data={"event_type":args.event})
        print("Wake request sent. GitHub completion is not a Minecraft readiness receipt.")
    elif args.mode=="stop":
        state=read_json(paths["runtime"],{})
        if not state.get("request_id"):raise RuntimeError("No owned runtime")
        atomic(paths["control"],{"request_id":state["request_id"],"command":"stop"})
        print("Graceful stop requested for Minecraft only.")
    else:
        print(json.dumps({"settings":{"idle_seconds":c.get("idle_seconds",1800),"poll_seconds":c.get("poll_seconds",30)},"supervisor":read_json(paths["supervisor"],{}),"listener":read_json(paths["listener"],{}),"runtime":read_json(paths["runtime"],{})},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
