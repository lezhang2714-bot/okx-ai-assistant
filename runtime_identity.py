"""Runtime build identity for monitor/web startup logs."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Dict


def get_runtime_identity(module_file: str) -> Dict[str, Any]:
    path = Path(module_file).resolve()
    label = f"{path.parent.name}/{path.name}"
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        digest = "unknown"
    git_rev = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_rev = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "path": str(path),
        "label": label,
        "sha256_12": digest,
        "git_rev": git_rev,
    }


def format_runtime_identity(module_file: str) -> str:
    info = get_runtime_identity(module_file)
    return f"{info['label']} git={info['git_rev']} sha={info['sha256_12']} path={info['path']}"
