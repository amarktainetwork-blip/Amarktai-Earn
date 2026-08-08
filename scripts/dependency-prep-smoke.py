from __future__ import annotations

import hashlib
import json
import os

from sandbox_broker.server import _run, prepare_dependencies


image = os.getenv("SANDBOX_AGENT_IMAGE", "amarktai-earn-sandbox:phase8c")
repo_volume = os.environ["SANDBOX_REPOSITORY_VOLUME"]
snapshot_rel = "1234567890abcdef1234567890abcdef/" + "a" * 40
package = json.dumps({"name": "amarktai-dependency-smoke", "version": "1.0.0"}, separators=(",", ":")).encode()
lock = json.dumps({
    "name": "amarktai-dependency-smoke", "version": "1.0.0", "lockfileVersion": 3,
    "requires": True, "packages": {"": {"name": "amarktai-dependency-smoke", "version": "1.0.0"}},
}, separators=(",", ":")).encode()
digest = hashlib.sha256(package + b"\0" + lock).hexdigest()
cache_key = ""
_run(["docker", "volume", "create", repo_volume], timeout=30)
try:
    script = "import os,pathlib; p=pathlib.Path('/src')/os.environ['REL']; p.mkdir(parents=True); (p/'package.json').write_bytes(bytes.fromhex(os.environ['PACKAGE'])); (p/'package-lock.json').write_bytes(bytes.fromhex(os.environ['LOCK']))"
    _run([
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "0:0", "-v", f"{repo_volume}:/src:rw",
        "-e", f"REL={snapshot_rel}", "-e", f"PACKAGE={package.hex()}", "-e", f"LOCK={lock.hex()}",
        image, "python", "-c", script,
    ], timeout=60)
    result = prepare_dependencies({
        "snapshot_rel": snapshot_rel, "ecosystem": "node", "manifest_path": "package-lock.json", "manifest_hash": digest,
    })
    cache_key = result["cache_key"]
    assert result["file_count"] > 0 and result["total_bytes"] >= 0
    mounted = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
        "-v", f"{cache_key}:/opt/amarktai-dependencies:ro", image, "test", "-f", "/opt/amarktai-dependencies/.ready",
    ], timeout=60, check=False)
    assert mounted.returncode == 0
    print(json.dumps(result, sort_keys=True))
finally:
    if cache_key:
        _run(["docker", "volume", "rm", "-f", cache_key], timeout=30, check=False)
    _run(["docker", "volume", "rm", "-f", repo_volume], timeout=30, check=False)
