"""Operations wrapper uses the existing deployment and publishes only complete output."""

import os
from pathlib import Path
import subprocess
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "export-audit.sh"


@pytest.mark.parametrize("managed", [True, False])
def test_export_from_another_directory(tmp_path, managed):
    deployment = tmp_path / "hospital deployment"
    deployment.mkdir()
    (deployment / ".env").write_text("JWT_SECRET=not-sourced\n")
    log = tmp_path / "calls"
    command = '#!/usr/bin/env bash\nprintf "%s\\n" "$PWD" "$@" > "$AUDIT_CALLS"\nprintf "fixture archive bytes"\n'
    if managed:
        (deployment / "manage-offline.sh").write_text(command)
    else:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(command)
        docker.chmod(0o755)
    env = {
        **os.environ,
        "AUDIT_CALLS": str(log),
        "PATH": str(tmp_path / "bin") + ":" + os.environ["PATH"],
    }
    output = tmp_path / "result with spaces.zip"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(deployment),
            "--dataset-id",
            "train_abc",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "fixture archive bytes"
    assert (
        "exec\n-T\nbackend\npython\n-m\napp.audit_export\n--dataset-id\ntrain_abc"
        in log.read_text()
    )
    again = subprocess.run(
        ["bash", str(SCRIPT), str(deployment), "--output", str(output)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert again.returncode != 0 and output.read_text() == "fixture archive bytes"


def test_failed_stream_never_publishes_or_leaves_partial(tmp_path):
    d = tmp_path / "deployment"
    d.mkdir()
    (d / "manage-offline.sh").write_text(
        "#!/usr/bin/env bash\nprintf partial\nexit 7\n"
    )
    out = tmp_path / "audit.zip"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(d), "--output", str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7 and not out.exists()
    assert not list(tmp_path.glob(".quality-audit.*"))


def test_lookup_json_passthrough_and_db_override_rejected(tmp_path):
    d = tmp_path / "deployment"
    d.mkdir()
    (d / "manage-offline.sh").write_text(
        '#!/usr/bin/env bash\nprintf "{\\"found\\":true}\\n"\n'
    )
    r = subprocess.run(
        ["bash", str(SCRIPT), str(d), "--lookup", "frame.jpg"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and r.stdout.strip() == '{"found":true}'
    assert not list(tmp_path.glob("*.zip"))
    for override in (["--db", "other.db"], ["--db=other.db"]):
        assert (
            subprocess.run(
                ["bash", str(SCRIPT), str(d), *override], capture_output=True
            ).returncode
            != 0
        )
