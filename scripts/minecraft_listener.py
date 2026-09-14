"""Consume trusted GitHub wake runs; own Minecraft, never the host power state.
Only stdlib + the user's existing gh login are needed. Runtime files stay outside Git.
"""
import argparse, contextlib, ctypes, hashlib, json, logging, os, socket, struct
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

def eligible(run, config, activated, seen, now=None):
    now = now or utcnow()
    if str(run.get("id")) in seen: return False
    if run.get("conclusion") != "success" or run.get("status") != "completed": return False
    if run.get("event") not in ("workflow_dispatch", "repository_dispatch"): return False
    if run.get("head_branch") != config["branch"]: return False
    if run.get("path", "").split("@")[0] != ".github/workflows/" + config["workflow"]: return False
    if run.get("repository", {}).get("full_name") != config["repository"]: return False
    if run.get("actor", {}).get("login") not in config["allowed_actors"]: return False
    try: created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError): return False
    age = (now-created).total_seconds()
    return activated <= created and 0 <= age <= config.get("request_ttl_seconds", 600)

class GitHub:
    def __init__(self, gh): self.gh = gh
    def api(self, path, method="GET", data=None):
        p = subprocess.run([self.gh, "auth", "token"], capture_output=True, creationflags=HIDDEN, timeout=20)
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
        "server_log":"server.log"}.items()}

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

def listen(c, config_file):
    paths=runtime_paths(c); gh=GitHub(c["gh"])
    with lock(paths["listener_lock"]):
        state=read_json(paths["listener"],None)
        if state is None:
            state={"activated_utc":utcnow().isoformat(),"seen":[],"requests":[]}
            atomic(paths["listener"],state)
        activated=datetime.fromisoformat(state["activated_utc"])
        LOG.info("Listener active; no historical requests before %s",activated)
        while True:
            try:
                d=gh.api(f"repos/{c['repository']}/actions/workflows/{c['workflow']}/runs?status=completed&per_page=20")
                for run in reversed(d.get("workflow_runs",[])):
                    if not eligible(run,c,activated,state["seen"]): continue
                    # Consume durably BEFORE spawning: crash means fail-closed, never replay.
                    rid=str(run["id"]); state["seen"]=(state["seen"]+[rid])[-200:]
                    state["requests"]=(state["requests"]+[{"id":rid,"accepted_utc":utcnow().isoformat()}])[-100:]
                    atomic(paths["listener"],state)
                    result=start_worker(c,config_file,rid)
                    state["requests"][-1]["result"]=result; atomic(paths["listener"],state)
                    LOG.info("Request %s: %s",rid,result)
                state["last_poll_utc"]=utcnow().isoformat(); state.pop("error",None)
                atomic(paths["listener"],state)
            except Exception as exc:
                # Never log response bodies or credential-bearing command lines.
                LOG.warning("Poll failed (%s); leaving all data untouched",type(exc).__name__)
                state["error"]=type(exc).__name__; atomic(paths["listener"],state)
            time.sleep(c.get("poll_seconds",30))

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

def worker(c,request_id):
    paths=runtime_paths(c)
    with lock(paths["worker_lock"]):
        previous=read_json(paths["runtime"],{})
        if port_busy(c["port"]) or pid_alive(previous.get("java_pid")):
            raise RuntimeError("Existing Minecraft process; refusing duplicate start")
        state={"request_id":request_id,"worker_pid":os.getpid(),"java_pid":None,"phase":"preflight",
               "started_utc":utcnow().isoformat(),"players":None}
        atomic(paths["runtime"],state)
        proc=None; stop_sent=False
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
            last_player=time.monotonic(); ready_at=None
            while proc.poll() is None:
                control=read_json(paths["control"],{})
                requested_stop=control.get("request_id")==request_id and control.get("command")=="stop"
                try:
                    info=status(port=c["port"]); players=info["players"]["online"]
                    if ready_at is None: ready_at=time.monotonic(); last_player=ready_at
                    state.update(phase="stopping" if stop_sent else "ready",players=players,status_utc=utcnow().isoformat())
                    if players: last_player=time.monotonic()
                    idle=ready_at is not None and time.monotonic()-last_player >= c.get("idle_seconds",1800)
                except Exception:
                    # A failed status probe is NOT evidence of an empty server.
                    state.update(players=None); idle=False
                if (requested_stop or idle) and not stop_sent:
                    proc.stdin.write("save-all flush\nstop\n");proc.stdin.flush();stop_sent=True
                    state["phase"]="stopping"; LOG.info("Requested graceful Minecraft-only stop")
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
    a=argparse.ArgumentParser(); a.add_argument("mode",choices=["listen","worker","request","status","stop"])
    a.add_argument("--config",required=True);a.add_argument("--request-id",default="manual")
    args=a.parse_args();config_file=Path(args.config).resolve();c=read_json(config_file)
    paths=runtime_paths(c)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(paths["log"],encoding="utf-8")])
    if args.mode=="listen": listen(c,config_file)
    elif args.mode=="worker": worker(c,args.request_id)
    elif args.mode=="request":
        GitHub(c["gh"]).api(f"repos/{c['repository']}/dispatches",method="POST",data={"event_type":"start-minecraft"})
        print("Wake request sent. GitHub completion is not a Minecraft readiness receipt.")
    elif args.mode=="stop":
        state=read_json(paths["runtime"],{})
        if not state.get("request_id"):raise RuntimeError("No owned runtime")
        atomic(paths["control"],{"request_id":state["request_id"],"command":"stop"})
        print("Graceful stop requested for Minecraft only.")
    else:
        print(json.dumps({"listener":read_json(paths["listener"],{}),"runtime":read_json(paths["runtime"],{})},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
