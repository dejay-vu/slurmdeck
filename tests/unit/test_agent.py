"""In-process behavioral tests of the remote agent (run against tmp run roots)."""

from __future__ import annotations

import json
import os
import re
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from slurmdeck.agent import protocol
from slurmdeck.transport import parse_json_lines

AGENT = Path("src/slurmdeck/agent/agent.py").resolve()


def _make_run_root(tmp_path: Path, task: dict, *, activation: str = "") -> Path:
    run_root = tmp_path / "run"
    (run_root / "results").mkdir(parents=True)
    (run_root / protocol.TASKS_FILE).write_text(json.dumps(task) + "\n", encoding="utf-8")
    (run_root / protocol.ACTIVATION_FILE).write_text(activation, encoding="utf-8")
    return run_root


def _exec(run_root: Path, *, index: int = 0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(AGENT),
            "exec",
            "--run-root",
            str(run_root),
            "--code-dir",
            str(run_root),
            "--index",
            str(index),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "SLURM_ARRAY_JOB_ID": "77", "SLURM_ARRAY_TASK_ID": str(index), **(env or {})},
    )


def _status(run_root: Path, task_id: str = "000") -> dict:
    return json.loads((run_root / "results" / task_id / protocol.STATUS_FILE).read_text(encoding="utf-8"))


def _py_task(code: str, **extra) -> dict:
    task = {
        "index": 0,
        "task_id": "000",
        "name": "t",
        "argv": [sys.executable, "-c", code],
        "env": {},
        "config": None,
        "result_dir": "results/000",
    }
    task.update(extra)
    return task


class TestExec:
    def test_success_writes_completed_status_with_slurm_id(self, tmp_path):
        run_root = _make_run_root(tmp_path, _py_task("print('ok')"))
        proc = _exec(run_root)
        assert proc.returncode == 0
        status = _status(run_root)
        assert status["state"] == "COMPLETED"
        assert status["exit_code"] == 0
        assert status["slurm_job_id"] == "77_0"
        assert status["schema_version"] == protocol.STATUS_SCHEMA_VERSION
        assert status["started_at"] and status["ended_at"]

    def test_failure_records_exit_code_and_reason(self, tmp_path):
        run_root = _make_run_root(tmp_path, _py_task("raise SystemExit(3)"))
        proc = _exec(run_root)
        assert proc.returncode == 3
        status = _status(run_root)
        assert status["state"] == "FAILED"
        assert status["exit_code"] == 3
        assert "code 3" in status["reason"]

    def test_task_env_beats_activation_env(self, tmp_path):
        # regression: activation used to run after task-env exports and clobber them
        code = (
            "import os,pathlib;pathlib.Path(os.environ['SLURMDECK_OUTPUT_DIR'],'env.txt')"
            ".write_text(os.environ['FOO']+os.environ['BAR'])"
        )
        task = _py_task(code, env={"FOO": "task"})
        run_root = _make_run_root(tmp_path, task, activation="export FOO=activation\nexport BAR=activation\n")
        proc = _exec(run_root)
        assert proc.returncode == 0, proc.stderr
        assert (run_root / "results/000/env.txt").read_text() == "taskactivation"

    def test_activation_failure_fails_before_running_command(self, tmp_path):
        # regression: partial `eval` of a failed setup used to let the task run
        # half-configured and report COMPLETED
        code = "import pathlib;pathlib.Path('should_not_exist.txt').write_text('x')"
        run_root = _make_run_root(tmp_path, _py_task(code), activation="echo pre\nfalse\necho post\n")
        proc = _exec(run_root)
        assert proc.returncode == 1
        status = _status(run_root)
        assert status["state"] == "FAILED"
        assert "activation failed" in status["reason"]
        assert not (run_root / "should_not_exist.txt").exists()

    def test_shell_mode_runs_without_login_shell(self, tmp_path):
        task = _py_task("", argv=None)
        task["shell"] = f'"{sys.executable}" -c "print(1)" && echo done > "$SLURMDECK_OUTPUT_DIR/shell.txt"'
        task["argv"] = None
        run_root = _make_run_root(tmp_path, task)
        proc = _exec(run_root)
        assert proc.returncode == 0, proc.stderr
        assert (run_root / "results/000/shell.txt").read_text().strip() == "done"

    def test_missing_command_reports_failed(self, tmp_path):
        run_root = _make_run_root(
            tmp_path,
            _py_task(
                "",
            )
            | {"argv": ["/nonexistent/prog"]},
        )
        proc = _exec(run_root)
        assert proc.returncode == 127
        status = _status(run_root)
        assert status["state"] == "FAILED"
        assert "could not start" in status["reason"]

    def test_sigterm_writes_killed_status(self, tmp_path):
        run_root = _make_run_root(tmp_path, _py_task("import time; time.sleep(30)"))
        proc = subprocess.Popen(
            [
                sys.executable,
                str(AGENT),
                "exec",
                "--run-root",
                str(run_root),
                "--code-dir",
                str(run_root),
                "--index",
                "0",
            ],
            env={"PATH": "/usr/bin:/bin"},
        )
        deadline = time.monotonic() + 10
        status_path = run_root / "results/000" / protocol.STATUS_FILE
        while time.monotonic() < deadline and not status_path.exists():
            time.sleep(0.05)
        time.sleep(0.3)  # let the child start
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        status = _status(run_root)
        assert status["state"] == "KILLED"
        assert "signal" in status["reason"]

    def test_started_at_preserved_across_rewrites(self, tmp_path):
        run_root = _make_run_root(tmp_path, _py_task("print('ok')"))
        _exec(run_root)
        status = _status(run_root)
        assert status["started_at"] <= status["ended_at"]


class TestScan:
    @staticmethod
    def _scheduler_payloads(tmp_path: Path, scripts: dict[str, str]) -> list[dict]:
        shims = tmp_path / "shims"
        shims.mkdir()
        for name, script in scripts.items():
            path = shims / name
            path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
            path.chmod(0o755)
        proc = subprocess.run(
            [
                sys.executable,
                str(AGENT),
                "scan",
                "--base",
                str(tmp_path / "base"),
                "--jobs",
                "999",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": str(shims)},
        )
        return [
            payload
            for payload in parse_json_lines(proc.stdout)
            if payload["kind"] in {protocol.SCAN_KIND_SQUEUE, protocol.SCAN_KIND_SACCT}
        ]

    def test_scheduler_query_payload_distinguishes_successful_empty_output(self, tmp_path):
        payloads = self._scheduler_payloads(tmp_path, {"squeue": "exit 0", "sacct": "exit 0"})

        assert [payload["source"] for payload in payloads] == ["squeue", "sacct"]
        assert all(payload["returncode"] == 0 for payload in payloads)
        assert all(payload["output"] == "" for payload in payloads)
        assert all(payload["stderr"] == "" for payload in payloads)
        assert all(payload["error"] == "" for payload in payloads)
        assert all(isinstance(payload["observed_at"], float) for payload in payloads)

    def test_scheduler_query_payload_reports_missing_executable(self, tmp_path):
        payloads = self._scheduler_payloads(tmp_path, {})

        assert all(payload["returncode"] is None for payload in payloads)
        assert all(payload["output"] == "" for payload in payloads)
        assert all(payload["error"] for payload in payloads)

    def test_scheduler_query_payload_reports_nonzero_and_stderr(self, tmp_path):
        payloads = self._scheduler_payloads(
            tmp_path,
            {
                "squeue": "echo queue-broken >&2\nexit 7",
                "sacct": "echo accounting-broken >&2\nexit 8",
            },
        )

        assert [payload["returncode"] for payload in payloads] == [7, 8]
        assert [payload["stderr"].strip() for payload in payloads] == ["queue-broken", "accounting-broken"]
        assert all(payload["error"] for payload in payloads)

    def test_scheduler_query_payload_reports_timeout(self, monkeypatch):
        namespace = runpy.run_path(str(AGENT))
        query = namespace["_slurm_query"]

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["squeue"], 120, stderr="query hung")

        monkeypatch.setattr(query.__globals__["subprocess"], "run", timeout)
        payload = query(["squeue"], "squeue")

        assert payload["returncode"] is None
        assert payload["stderr"] == "query hung"
        assert "timed out" in payload["error"]

    def test_scan_since_filters_by_mtime(self, tmp_path):
        base = tmp_path / "base"
        for run_id, task_id in [("r1", "000"), ("r1", "001"), ("r2", "000")]:
            result = base / "runs" / run_id / "results" / task_id
            result.mkdir(parents=True)
            (result / "status.json").write_text(json.dumps({"task_id": task_id, "state": "COMPLETED"}))
        old = base / "runs/r1/results/000/status.json"
        cutoff = time.time() - 100
        import os

        os.utime(old, (cutoff - 50, cutoff - 50))

        proc = subprocess.run(
            [
                sys.executable,
                str(AGENT),
                "scan",
                "--base",
                str(base),
                "--run",
                "r1",
                "--run",
                "r2",
                "--since",
                str(cutoff),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        payloads = parse_json_lines(proc.stdout)
        assert payloads[0]["kind"] == protocol.SCAN_KIND_HEADER
        tasks = [p for p in payloads if p["kind"] == protocol.SCAN_KIND_TASK]
        assert {(p["run_id"], p["task_id"]) for p in tasks} == {("r1", "001"), ("r2", "000")}
        assert all(p["mtime"] > cutoff for p in tasks)


class TestRunSubmission:
    TOKEN = "a" * 64

    @staticmethod
    def _shim(path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def _invoke(
        self,
        base: Path,
        shim_dir: Path,
        command: str,
        *,
        timeout: str = "2",
        prepare_snapshot: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        run_id = "run-1"
        script = base / "runs" / run_id / protocol.SBATCH_FILE
        snapshot_hash = "b" * 64
        args = [
            sys.executable,
            str(AGENT),
            command,
            "--base",
            str(base),
            "--run-id",
            run_id,
            "--token",
            self.TOKEN,
            "--job-name",
            f"sd-{self.TOKEN[-12:]}",
        ]
        if command == "submit-run":
            if prepare_snapshot:
                (script.parent / protocol.RUN_MANIFEST_FILE).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "snapshot_hash": snapshot_hash,
                        }
                    ),
                    encoding="utf-8",
                )
                marker = base / "snapshots" / snapshot_hash / ".complete.json"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "hash": snapshot_hash,
                            "created_at": "2000-01-01T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )
            args += [
                "--script",
                str(script),
                "--snapshot-hash",
                snapshot_hash,
                "--timeout",
                timeout,
            ]
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
        )

    def test_submit_is_idempotent_and_persists_full_token_metadata(self, tmp_path):
        base = tmp_path / "base"
        script = base / "runs" / "run-1" / protocol.SBATCH_FILE
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        counter = tmp_path / "sbatch.count"
        args_log = tmp_path / "sbatch.args"
        self._shim(
            shim_dir / "sbatch",
            f'count=$(cat "{counter}" 2>/dev/null || echo 0)\n'
            f'echo $((count + 1)) > "{counter}"\n'
            f'echo "$*" >> "{args_log}"\n'
            "echo '12345;cluster'",
        )

        first = self._invoke(base, shim_dir, "submit-run")
        second = self._invoke(base, shim_dir, "submit-run")

        assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
        first_payload = parse_json_lines(first.stdout)[0]
        second_payload = parse_json_lines(second.stdout)[0]
        assert first_payload["status"] == second_payload["status"] == "submitted"
        assert first_payload["job_id"] == second_payload["job_id"] == "12345"
        assert first_payload["source"] == "sbatch"
        assert second_payload["source"] == "receipt"
        assert counter.read_text(encoding="utf-8").strip() == "1"
        sbatch_args = args_log.read_text(encoding="utf-8")
        assert f"--comment={self.TOKEN}" in sbatch_args
        assert f"--job-name=sd-{self.TOKEN[-12:]}" in sbatch_args
        receipt = json.loads((base / "receipts" / "run" / f"{self.TOKEN}.json").read_text())
        assert receipt["schema_version"] == 1
        assert receipt["token"] == self.TOKEN
        assert receipt["run_id"] == "run-1"
        assert receipt["snapshot_hash"] == "b" * 64
        assert receipt["status"] == "submitted"
        assert receipt["job_id"] == "12345"
        assert (base / "locks" / "run" / f"{self.TOKEN}.lock").is_file()
        assert (base / "locks" / "snapshot-gc.lock").is_file()

    def test_submit_refuses_to_call_sbatch_without_the_committed_snapshot(self, tmp_path):
        base = tmp_path / "base"
        script = base / "runs" / "run-1" / protocol.SBATCH_FILE
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        called = tmp_path / "sbatch.called"
        self._shim(shim_dir / "sbatch", f'touch "{called}"\necho 12345')

        result = self._invoke(base, shim_dir, "submit-run", prepare_snapshot=False)

        payload = parse_json_lines(result.stdout)[0]
        assert payload["status"] == "failed"
        assert "snapshot" in payload["error"]
        assert not called.exists()

    def test_submit_failure_and_timeout_are_receipted_without_resubmission(self, tmp_path):
        base = tmp_path / "base"
        script = base / "runs" / "run-1" / protocol.SBATCH_FILE
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        counter = tmp_path / "sbatch.count"
        self._shim(
            shim_dir / "sbatch",
            f'count=$(cat "{counter}" 2>/dev/null || echo 0)\n'
            f'echo $((count + 1)) > "{counter}"\n'
            "echo rejected >&2\nexit 9",
        )

        failed = self._invoke(base, shim_dir, "submit-run")
        repeated = self._invoke(base, shim_dir, "submit-run")

        assert parse_json_lines(failed.stdout)[0]["status"] == "failed"
        assert parse_json_lines(repeated.stdout)[0]["status"] == "failed"
        assert counter.read_text(encoding="utf-8").strip() == "1"

        other_token = "c" * 64
        self.TOKEN = other_token
        self._shim(
            shim_dir / "sbatch",
            f'count=$(cat "{counter}" 2>/dev/null || echo 0)\necho $((count + 1)) > "{counter}"\nsleep 1\necho 999',
        )
        unknown = self._invoke(base, shim_dir, "submit-run", timeout="0.05")
        repeated_unknown = self._invoke(base, shim_dir, "submit-run", timeout="0.05")

        assert parse_json_lines(unknown.stdout)[0]["status"] == "unknown"
        assert parse_json_lines(repeated_unknown.stdout)[0]["status"] == "unknown"
        assert counter.read_text(encoding="utf-8").strip() == "2"

    def test_receipt_write_failure_after_sbatch_is_unknown_not_retryable(self, tmp_path, monkeypatch, capsys):
        namespace = runpy.run_path(str(AGENT))
        submit = namespace["cmd_submit_run"]
        base = tmp_path / "base"
        script = base / "runs" / "run-1" / protocol.SBATCH_FILE
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        (script.parent / protocol.RUN_MANIFEST_FILE).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "snapshot_hash": "b" * 64,
                }
            ),
            encoding="utf-8",
        )
        marker = base / "snapshots" / ("b" * 64) / ".complete.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hash": "b" * 64,
                    "created_at": "2000-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        real_write = submit.__globals__["_atomic_write_json"]
        writes = 0

        def fail_final_receipt(path, payload):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("injected final receipt failure")
            real_write(path, payload)

        monkeypatch.setitem(submit.__globals__, "_atomic_write_json", fail_final_receipt)
        monkeypatch.setattr(
            submit.__globals__["subprocess"],
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(["sbatch"], 0, "81234\n", ""),
        )
        args = SimpleNamespace(
            base=str(base),
            run_id="run-1",
            token=self.TOKEN,
            job_name=f"sd-{self.TOKEN[-12:]}",
            script=str(script),
            snapshot_hash="b" * 64,
            timeout=2.0,
        )

        assert submit(args) == 0

        payload = parse_json_lines(capsys.readouterr().out)[0]
        assert payload["status"] == "unknown"
        assert "final receipt failure" in payload["error"]
        receipt = json.loads((base / "receipts" / "run" / f"{self.TOKEN}.json").read_text())
        assert receipt["status"] == "submitting"

    @pytest.mark.parametrize("match_source", ["comment", "job_name"])
    def test_reconcile_finds_a_job_by_comment_then_job_name_fallback(self, tmp_path, match_source):
        base = tmp_path / "base"
        (base / "runs" / "run-1").mkdir(parents=True)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        job_name = f"sd-{self.TOKEN[-12:]}"
        accounting_called = tmp_path / "sacct.called"
        if match_source == "comment":
            queue = f"echo '700|{self.TOKEN}|other'"
            accounting = f'touch "{accounting_called}"'
        else:
            queue = "exit 0"
            accounting = f"echo '701||{job_name}'"
        self._shim(shim_dir / "squeue", queue)
        self._shim(shim_dir / "sacct", accounting)

        result = self._invoke(base, shim_dir, "reconcile-run")

        assert result.returncode == 0, result.stderr
        payload = parse_json_lines(result.stdout)[0]
        assert payload["status"] == "submitted"
        assert payload["job_id"] == ("700" if match_source == "comment" else "701")
        assert payload["source"] == match_source
        if match_source == "comment":
            assert not accounting_called.exists()


class TestProtocolSync:
    """The agent cannot import slurmdeck, so its constants are duplicated by design."""

    def test_constants_match(self):
        source = protocol.agent_source()
        assert f"AGENT_VERSION = {protocol.AGENT_VERSION}" in source
        assert f"STATUS_SCHEMA_VERSION = {protocol.STATUS_SCHEMA_VERSION}" in source
        assert json.dumps(protocol.JSON_PREFIX)[1:-1].replace("\\t", "\\t") in source or "SLURMDECK_JSON\\t" in source
        assert f'"{protocol.SCAN_KIND_HEADER}"' in source
        assert f'"{protocol.RUN_SUBMISSION_KIND}"' in source
        assert f'"{protocol.RUN_CLEAN_KIND}"' in source
        assert f'"{protocol.SNAPSHOT_LIFECYCLE_KIND}"' in source
        assert re.search(r"python\s*>=?\s*3\.8", source) or "python >= 3.8" in source

    def test_agent_is_stdlib_only(self):
        source = protocol.agent_source()
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                module = line.split()[1].split(".")[0]
                assert module in {
                    "argparse",
                    "calendar",
                    "contextlib",
                    "fcntl",
                    "json",
                    "os",
                    "re",
                    "shutil",
                    "signal",
                    "socket",
                    "subprocess",
                    "sys",
                    "time",
                }, f"non-stdlib import in agent: {line}"
