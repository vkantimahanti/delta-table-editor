"""Run Data Manager deploy_config.py --dry-run against generated YAML."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .config import data_editor_root

logger = logging.getLogger("agent_builder.validate")


def run_deploy_dry_run(yaml_path: Path) -> tuple[bool, str]:
    root = data_editor_root()
    script = root / "scripts" / "deploy_config.py"
    if not script.is_file():
        return False, f"deploy_config.py not found at {script}"

    rel = yaml_path
    try:
        rel = yaml_path.resolve().relative_to(root.resolve())
    except ValueError:
        pass

    cmd = [sys.executable, str(script), "--file", str(rel), "--dry-run"]
    logger.info("Validating: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out
