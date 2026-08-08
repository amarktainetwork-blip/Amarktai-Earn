from __future__ import annotations

import hashlib
import json
import os

from sandbox_broker.server import _run, prepare_dependencies


image = os.getenv("SANDBOX_AGENT_IMAGE", "amarktai-earn-sandbox:phase8c")
repo_volume = os.environ["SANDBOX_REPOSITORY_VOLUME"]
node_rel = "1234567890abcdef1234567890abcdef/" + "a" * 40
python_rel = "abcdef1234567890abcdef1234567890/" + "b" * 40
package = json.dumps({"name": "amarktai-dependency-smoke", "version": "1.0.0"}, separators=(",", ":")).encode()
lock = json.dumps({
    "name": "amarktai-dependency-smoke", "version": "1.0.0", "lockfileVersion": 3,
    "requires": True, "packages": {"": {"name": "amarktai-dependency-smoke", "version": "1.0.0"}},
}, separators=(",", ":")).encode()
node_digest = hashlib.sha256(package + b"\0" + lock).hexdigest()
requirements = b"idna==3.15 --hash=sha256:048adeaf8c2d788c40fee287673ccaa74c24ffd8dcf09ffa555a2fbb59f10ac8\n"
python_digest = hashlib.sha256(requirements).hexdigest()
cache_keys = []
_run(["docker", "volume", "create", repo_volume], timeout=30)
try:
    script = "import os,pathlib; n=pathlib.Path('/src')/os.environ['NODE_REL']; n.mkdir(parents=True); (n/'package.json').write_bytes(bytes.fromhex(os.environ['PACKAGE'])); (n/'package-lock.json').write_bytes(bytes.fromhex(os.environ['LOCK'])); p=pathlib.Path('/src')/os.environ['PYTHON_REL']; p.mkdir(parents=True); (p/'requirements.txt').write_bytes(bytes.fromhex(os.environ['REQUIREMENTS']))"
    _run([
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "0:0", "-v", f"{repo_volume}:/src:rw",
        "-e", f"NODE_REL={node_rel}", "-e", f"PYTHON_REL={python_rel}", "-e", f"PACKAGE={package.hex()}",
        "-e", f"LOCK={lock.hex()}", "-e", f"REQUIREMENTS={requirements.hex()}",
        image, "python", "-c", script,
    ], timeout=60)
    node_result = prepare_dependencies({
        "snapshot_rel": node_rel, "ecosystem": "node", "manifest_path": "package-lock.json", "manifest_hash": node_digest,
    })
    cache_keys.append(node_result["cache_key"])
    assert node_result["file_count"] > 0 and node_result["total_bytes"] >= 0
    mounted = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
        "-v", f"{node_result['cache_key']}:/opt/amarktai-dependencies:ro", image, "test", "-f", "/opt/amarktai-dependencies/.ready",
    ], timeout=60, check=False)
    assert mounted.returncode == 0

    python_result = prepare_dependencies({
        "snapshot_rel": python_rel, "ecosystem": "python", "manifest_path": "requirements.txt", "manifest_hash": python_digest,
    })
    cache_keys.append(python_result["cache_key"])
    imported = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
        "-v", f"{python_result['cache_key']}:/opt/amarktai-dependencies:ro",
        "-e", "PYTHONPATH=/opt/amarktai-dependencies/site-packages", image, "python", "-c",
        "import idna; assert idna.__version__ == '3.15'; assert idna.__file__.startswith('/opt/amarktai-dependencies/site-packages/')",
    ], timeout=60, check=False)
    assert imported.returncode == 0
    print(json.dumps({"node": node_result, "python": python_result}, sort_keys=True))
finally:
    for cache_key in cache_keys:
        _run(["docker", "volume", "rm", "-f", cache_key], timeout=30, check=False)
    _run(["docker", "volume", "rm", "-f", repo_volume], timeout=30, check=False)
