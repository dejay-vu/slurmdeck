"""End-to-end CLI test through real subprocesses and PATH-shimmed ssh/rsync.

The fake ``ssh`` executes the remote command locally (so the local machine
plays the cluster), the fake ``rsync`` strips ``host:`` prefixes and delegates
to the real rsync, and shim ``sbatch``/``squeue``/``sacct``/``scancel``
binaries emulate Slurm. Nothing here matches on slurmdeck internals beyond the
``sbatch --parsable`` contract, which is stable by design.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_SSH = """#!/usr/bin/env bash
# fake ssh: run the "remote" command locally
mode=""
dest=""
cmd=""
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    -o) i=$((i+2)); continue ;;
    -O) mode="${args[$((i+1))]}"; i=$((i+2)); continue ;;
    -M|-N|-f) i=$((i+1)); continue ;;
    *) if [ -z "$dest" ]; then dest="$a"; else cmd="$a"; fi; i=$((i+1)) ;;
  esac
done
[ -n "$mode" ] && exit 0
[ -z "$cmd" ] && exit 0
exec bash -c "$cmd"
"""

FAKE_RSYNC = """#!/usr/bin/env bash
# fake rsync: drop the ssh transport args, strip host: prefixes, run real rsync
echo "rsync $*" >> "$SLURMDECK_E2E_LOG"
args=()
skip=0
for a in "$@"; do
  if [ $skip -eq 1 ]; then skip=0; continue; fi
  case "$a" in
    -e) skip=1 ;;
    --timeout=*) ;;
    *@*:*) args+=("${a#*:}") ;;
    *) args+=("$a") ;;
  esac
done
exec /usr/bin/rsync "${args[@]}"
"""

FAKE_SBATCH = """#!/usr/bin/env bash
echo "sbatch $*" >> "$SLURMDECK_E2E_LOG"
echo 424242
"""

FAKE_SQUEUE = """#!/usr/bin/env bash
echo "squeue $*" >> "$SLURMDECK_E2E_LOG"
exit 0
"""

FAKE_SACCT = """#!/usr/bin/env bash
echo "sacct $*" >> "$SLURMDECK_E2E_LOG"
printf '424242_0|COMPLETED|0:0|None\\n'
"""

FAKE_SCANCEL = """#!/usr/bin/env bash
echo "scancel $*" >> "$SLURMDECK_E2E_LOG"
exit 0
"""


@pytest.fixture()
def e2e(tmp_path: Path):
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    for name, body in [
        ("ssh", FAKE_SSH),
        ("rsync", FAKE_RSYNC),
        ("sbatch", FAKE_SBATCH),
        ("squeue", FAKE_SQUEUE),
        ("sacct", FAKE_SACCT),
        ("scancel", FAKE_SCANCEL),
    ]:
        path = shim_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    project = tmp_path / "project"
    project.mkdir()
    (project / "train.py").write_text("print('hi')\n", encoding="utf-8")

    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg-runtime"),
        "SLURMDECK_E2E_LOG": str(log),
        "COLUMNS": "200",
    }
    (tmp_path / "xdg-runtime").mkdir()

    def cli(*args: str, cwd: Path = project, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "slurmdeck", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{args} failed rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    return cli, tmp_path, project, log


def test_full_lifecycle_over_shimmed_ssh(e2e):
    cli, tmp_path, project, log = e2e
    base = tmp_path / "cluster"

    cli("remote", "add", "hpc", "--host", "user@fake.example.com", "--base", str(base), "--use")
    cli("remote", "connect")
    assert (base / "runs").is_dir()  # created through the shimmed ssh python

    cli("init")
    assert (project / ".slurmdeck" / "project.yaml").exists()

    cli("submit", "--name", "demo", "--time", "00:10:00", "--", "python3", "train.py")
    listing = cli("run", "list", "--json")
    rows = json.loads(listing.stdout)["data"]
    assert len(rows) == 1
    run_id = rows[0]["id"]
    assert rows[0]["slurm_job_id"] == "424242"
    assert rows[0]["resources"]["time"] == "00:10:00"

    # snapshot + run dir made it to the "cluster" through the rsync shim
    remote_run = base / "runs" / run_id
    assert (remote_run / "submit.sbatch").exists()
    assert (remote_run / "agent.py").exists()
    assert (remote_run / "tasks.jsonl").exists()
    snapshots = list((base / "snapshots").iterdir())
    assert (snapshots[0] / "code" / "train.py").exists()
    assert "--parsable" in log.read_text()

    status = cli("run", "status", "--json")
    payload = json.loads(status.stdout)["data"]
    assert payload["summary"]["counts"] == {"COMPLETED": 1}  # via fake sacct

    cli("run", "cancel", "--yes")
    assert "scancel 424242" in log.read_text()


def test_doctor_and_errors_over_shims(e2e):
    cli, tmp_path, _project, log = e2e
    base = tmp_path / "cluster"
    cli("remote", "add", "hpc", "--host", "user@fake.example.com", "--base", str(base), "--use")
    cli("remote", "connect")
    cli("init")

    doctor = cli("doctor", "--json")
    checks = {item["name"]: item for item in json.loads(doctor.stdout)["data"]}
    assert checks["slurm"]["state"] == "OK"
    assert checks["base"]["state"] == "OK"
    assert checks["remote python3"]["state"] == "OK"

    # a typo'd option must be a usage error (exit 2), not a submitted job
    calls_before_typo = log.read_text()
    result = cli("submit", "--parittion", "gpu", "--", "python3", "train.py", check=False)
    assert result.returncode == 2
    assert log.read_text() == calls_before_typo

    # UserError funnel: exit 1 with hint on stderr
    result = cli("run", "status", check=False)
    assert result.returncode == 1
    assert "No runs" in result.stderr
