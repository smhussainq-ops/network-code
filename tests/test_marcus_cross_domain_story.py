from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_marcus_cross_domain_story_closes_only_after_signed_service_evidence():
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "marcus_cross_domain_assurance.py")],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["result"] == "passed"
    assert result["live_manager_lab"] is False
    assert result["safety"] == {
        "credentials_in_control_plane_jobs": False,
        "browser_evidence_spoof_blocked": True,
        "requester_self_approval_blocked": True,
        "rez_read_only": True,
        "human_approval_before_writes": True,
        "manager_success_not_service_success": True,
    }
    assert result["timeline"][4]["manager_push"] == "success"
    assert result["timeline"][4]["failed_domain"] == "routing"
    assert result["timeline"][-1]["status"] == "verified"
