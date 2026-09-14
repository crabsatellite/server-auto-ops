"""Cold backup of the canonical Minecraft checkout, including ignored files.
No source files are changed. Git history is saved separately as a bundle.
"""
import argparse, hashlib, json, os, subprocess, zipfile
from datetime import datetime, timezone
from pathlib import Path

def backup(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if not root.is_dir() or not destination.is_dir():
        raise RuntimeError("Both canonical root and existing destination must exist")
    if destination == root or root in destination.parents:
        raise RuntimeError("Backup destination must be outside the server checkout")
    # Fail closed when a Minecraft process owns the listening port.
    if os.name == "nt":
        probe = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            "if (Get-NetTCPConnection -LocalPort 25565 -State Listen -ErrorAction SilentlyContinue) {exit 2}"], capture_output=True)
        if probe.returncode:
            raise RuntimeError("Port 25565 is in use; stop and save the server first")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = destination / ("minecraft-preservation-" + stamp)
    archive = Path(str(prefix) + ".zip")
    bundle = Path(str(prefix) + ".bundle")
    receipt = Path(str(prefix) + ".json")
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    head = run("rev-parse", "HEAD").stdout.decode().strip()
    status = run("status", "--porcelain=v1", "--untracked-files=all").stdout.decode("utf-8")
    print("Saving Git history", flush=True)
    run("bundle", "create", str(bundle), "--all")
    run("bundle", "verify", str(bundle))
    inventory = []
    files = sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)
    print("Archiving", len(files), "files including ignored data", flush=True)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for n, path in enumerate(files):
            before = path.stat()
            if path.is_symlink():
                raise RuntimeError("Symlink requires explicit review: " + str(path))
            digest = hashlib.sha256()
            relative = path.relative_to(root).as_posix()
            with path.open("rb") as src, z.open(relative, "w", force_zip64=True) as dst:
                for chunk in iter(lambda: src.read(4 * 1024 * 1024), b""):
                    digest.update(chunk); dst.write(chunk)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("Source changed during backup: " + relative)
            inventory.append({"path": relative, "bytes": before.st_size, "sha256": digest.hexdigest()})
            if n % 2000 == 0: print("Archived", n, flush=True)
    print("Verifying every archived file against SHA-256 inventory", flush=True)
    with zipfile.ZipFile(archive) as z:
        if set(z.namelist()) != {x["path"] for x in inventory}:
            raise RuntimeError("Archive inventory mismatch")
        for row in inventory:
            digest = hashlib.sha256()
            with z.open(row["path"]) as src:
                for chunk in iter(lambda: src.read(4 * 1024 * 1024), b""): digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise RuntimeError("Archive hash mismatch: " + row["path"])
    def sha(path):
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""): h.update(chunk)
        return h.hexdigest()
    result = {"schema": 1, "verified": True, "created_utc": stamp, "canonical_root": str(root),
        "head": head, "working_tree_status": status, "archive": str(archive),
        "archive_sha256": sha(archive), "bundle": str(bundle), "bundle_sha256": sha(bundle),
        "file_count": len(inventory), "bytes": sum(x["bytes"] for x in inventory), "files": inventory,
        "scope": "Local checkout and Git refs only; not an assertion about the locked cloud disks."}
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "files"}, ensure_ascii=True), flush=True)
    print("RECEIPT", receipt, flush=True)

if __name__ == "__main__":
    a=argparse.ArgumentParser(); a.add_argument("root"); a.add_argument("destination"); p=a.parse_args()
    backup(p.root,p.destination)
