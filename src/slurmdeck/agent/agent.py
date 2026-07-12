#!/usr/bin/env python3
"""slurmdeck remote agent (stdlib-only, python >= 3.8).

One copy of this file is uploaded with every run; compute nodes execute
``agent.py exec`` from the sbatch script, and the local slurmdeck invokes
``scan`` by piping this source over ssh. It never templates or evals anything:
tasks arrive fully resolved in ``tasks.jsonl``.

Subcommands:
  exec      --run-root DIR --code-dir DIR --index N   run one task
  scan      --base DIR --run ID [--run ID ...] [--since EPOCH]
  submit-run --base DIR --run-id ID --token TOKEN --script FILE
  reconcile-run --base DIR --run-id ID --token TOKEN --job-name NAME
  clean-run --base DIR --run-id ID [--token TOKEN]
  snapshot-list|snapshot-gc --base DIR
"""

import argparse
import calendar
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

AGENT_VERSION = 1
STATUS_SCHEMA_VERSION = 1
JSON_PREFIX = "SLURMDECK_JSON\t"
ENV_DUMP_PREFIX = "SLURMDECK_ENV\t"
SCAN_KIND_HEADER = "slurmdeck.scan.v1"
SCAN_KIND_TASK = "task"
SCAN_KIND_SQUEUE = "squeue"
SCAN_KIND_SACCT = "sacct"
SCAN_KIND_ENV_DEPENDENCY = "env_dependency"
RUN_SUBMISSION_KIND = "slurmdeck.run-submission.v1"
RUN_RECEIPT_SCHEMA_VERSION = 1
RUN_CLEAN_KIND = "slurmdeck.run-clean.v1"
SNAPSHOT_LIFECYCLE_KIND = "slurmdeck.snapshot-lifecycle.v1"

TASKS_FILE = "tasks.jsonl"
RUN_MANIFEST_FILE = "run.json"
ACTIVATION_FILE = "activation.sh"
RESULTS_DIR = "results"
STATUS_FILE = "status.json"

STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"
STATE_KILLED = "KILLED"

_TOKEN_RE = re.compile(r"^[a-f0-9]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit(payload):
    sys.stdout.write(JSON_PREFIX + json.dumps(payload, sort_keys=True) + "\n")


def _write_status(result_dir, payload):
    """Atomically write status.json, preserving started_at across rewrites."""
    path = os.path.join(result_dir, STATUS_FILE)
    existing_started = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                existing_started = json.load(handle).get("started_at")
        except Exception:
            existing_started = None
    payload["schema_version"] = STATUS_SCHEMA_VERSION
    payload["agent_version"] = AGENT_VERSION
    payload["host"] = socket.gethostname()
    payload["slurm_job_id"] = _slurm_job_id()
    payload["started_at"] = existing_started or _utc_now()
    payload["ended_at"] = None if payload["state"] == STATE_RUNNING else _utc_now()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _slurm_job_id():
    array_job = os.environ.get("SLURM_ARRAY_JOB_ID")
    array_task = os.environ.get("SLURM_ARRAY_TASK_ID")
    if array_job and array_task is not None:
        return f"{array_job}_{array_task}"
    return os.environ.get("SLURM_JOB_ID", "")


def _load_task(run_root, index):
    path = os.path.join(run_root, TASKS_FILE)
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle):
            if line_number == index:
                return json.loads(line)
    raise SystemExit(f"task index {index} out of range in {path}")


def _capture_activation_env(run_root):
    """Source activation.sh in a clean shell and capture the resulting environment.

    Returns (env_dict or None, error_message or None). ``None`` env with no
    error means "no activation configured".
    """
    activation = os.path.join(run_root, ACTIVATION_FILE)
    if not os.path.exists(activation) or os.path.getsize(activation) == 0:
        return None, None
    dump = f'import json,os,sys;sys.stdout.write("{ENV_DUMP_PREFIX}"+json.dumps(dict(os.environ)))'
    script = 'set -e\nsource "$1"\n"$2" -c "$3"\n'
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script, "bash", activation, sys.executable, dump],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        return None, f"environment activation failed (rc={proc.returncode}): {tail}"
    for line in proc.stdout.splitlines():
        if line.startswith(ENV_DUMP_PREFIX):
            return json.loads(line[len(ENV_DUMP_PREFIX) :]), None
    return None, "environment activation produced no environment dump"


class _TaskRunner:
    def __init__(self, result_dir):
        self.result_dir = result_dir
        self.child = None
        self.killed_by = None

    def _handle_signal(self, signum, frame):
        self.killed_by = signum
        if self.child is not None and self.child.poll() is None:
            with contextlib.suppress(OSError):
                self.child.terminate()

    def run(self, command, env, cwd):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            signal.signal(signum, self._handle_signal)
        self.child = subprocess.Popen(command, env=env, cwd=cwd)
        rc = self.child.wait()
        return rc


def cmd_exec(args):
    run_root = os.path.abspath(args.run_root)
    task = _load_task(run_root, args.index)
    task_id = str(task["task_id"])
    result_dir = os.path.join(run_root, task["result_dir"])
    os.makedirs(result_dir, exist_ok=True)

    _write_status(result_dir, {"task_id": task_id, "state": STATE_RUNNING, "exit_code": None, "reason": ""})

    activation_env, activation_error = _capture_activation_env(run_root)
    if activation_error:
        _write_status(
            result_dir,
            {"task_id": task_id, "state": STATE_FAILED, "exit_code": None, "reason": activation_error},
        )
        sys.stderr.write(activation_error + "\n")
        return 1

    env = dict(activation_env if activation_env is not None else os.environ)
    config_path = os.path.join(run_root, task["config"]) if task.get("config") else ""
    env.update(
        {
            "SLURMDECK_RUN_ROOT": run_root,
            "SLURMDECK_TASK_ID": task_id,
            "SLURMDECK_CONFIG_PATH": config_path,
            "SLURMDECK_OUTPUT_DIR": result_dir,
        }
    )
    env.update({str(key): str(value) for key, value in (task.get("env") or {}).items()})

    if task.get("argv"):
        command = [str(token) for token in task["argv"]]
    else:
        command = ["bash", "--noprofile", "--norc", "-c", str(task["shell"])]

    runner = _TaskRunner(result_dir)
    try:
        rc = runner.run(command, env, args.code_dir)
    except OSError as exc:
        reason = f"could not start command: {exc}"
        _write_status(result_dir, {"task_id": task_id, "state": STATE_FAILED, "exit_code": None, "reason": reason})
        sys.stderr.write(reason + "\n")
        return 127

    if runner.killed_by is not None:
        reason = f"killed by signal {runner.killed_by} (timeout/preemption/cancel)"
        _write_status(result_dir, {"task_id": task_id, "state": STATE_KILLED, "exit_code": rc, "reason": reason})
        return 128 + runner.killed_by
    if rc == 0:
        _write_status(result_dir, {"task_id": task_id, "state": STATE_COMPLETED, "exit_code": 0, "reason": ""})
    else:
        reason = f"command exited with code {rc}"
        _write_status(result_dir, {"task_id": task_id, "state": STATE_FAILED, "exit_code": rc, "reason": reason})
    return rc


def _process_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _submission_paths(base, token):
    return (
        os.path.join(base, "receipts", "run", token + ".json"),
        os.path.join(base, "locks", "run", token + ".lock"),
    )


def _validate_submission_identity(base, run_id, token, job_name):
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("submission token must be 64 lowercase hexadecimal characters")
    if not _NAME_RE.fullmatch(run_id):
        raise ValueError("invalid run id")
    if not _NAME_RE.fullmatch(job_name):
        raise ValueError("invalid Slurm job name")
    return os.path.realpath(os.path.abspath(os.path.expanduser(base)))


def _read_receipt(path, token, run_id):
    if not os.path.exists(path):
        return None, ""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return None, f"unreadable submission receipt: {exc}"
    if not isinstance(payload, dict):
        return None, "submission receipt is not an object"
    if payload.get("schema_version") != RUN_RECEIPT_SCHEMA_VERSION:
        return None, "unsupported submission receipt schema"
    if payload.get("token") != token or payload.get("run_id") != run_id:
        return None, "submission receipt identity does not match"
    return payload, ""


def _emit_submission(receipt, source, *, status=None, error=""):
    payload = {
        "kind": RUN_SUBMISSION_KIND,
        "schema_version": RUN_RECEIPT_SCHEMA_VERSION,
        "status": status or receipt.get("status", "unknown"),
        "source": source,
        "token": receipt.get("token", ""),
        "run_id": receipt.get("run_id", ""),
        "job_name": receipt.get("job_name", ""),
        "job_id": receipt.get("job_id", ""),
        "error": error or receipt.get("error", ""),
    }
    _emit(payload)


def _receipt_record(args, base, *, status, error="", job_id=""):
    return {
        "schema_version": RUN_RECEIPT_SCHEMA_VERSION,
        "token": args.token,
        "run_id": args.run_id,
        "status": status,
        "job_id": job_id,
        "job_name": args.job_name,
        "script": getattr(args, "script", ""),
        "snapshot_hash": getattr(args, "snapshot_hash", ""),
        "base": base,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "error": error,
    }


def _parse_sbatch_job_id(stdout):
    for line in reversed(stdout.splitlines()):
        candidate = line.strip().split(";", 1)[0]
        if re.fullmatch(r"[0-9]+", candidate):
            return candidate
    return ""


@contextlib.contextmanager
def _with_submission_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json_object(path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _snapshot_references(base):
    references = {}  # type: dict[str, set[str]]
    runs_root = os.path.join(base, "runs")
    if os.path.isdir(runs_root):
        for run_id in sorted(os.listdir(runs_root)):
            run_dir = os.path.join(runs_root, run_id)
            if not os.path.isdir(run_dir) or os.path.islink(run_dir):
                continue
            manifest = _read_json_object(os.path.join(run_dir, RUN_MANIFEST_FILE))
            if manifest is None or manifest.get("schema_version") != 1:
                continue
            digest = manifest.get("snapshot_hash")
            stored_run_id = manifest.get("run_id")
            if isinstance(digest, str) and _TOKEN_RE.fullmatch(digest) and stored_run_id == run_id:
                references.setdefault(digest, set()).add("run:" + run_id)

    receipts_root = os.path.join(base, "receipts", "run")
    if os.path.isdir(receipts_root):
        for name in sorted(os.listdir(receipts_root)):
            if not name.endswith(".json"):
                continue
            token = name[:-5]
            if not _TOKEN_RE.fullmatch(token):
                continue
            receipt = _read_json_object(os.path.join(receipts_root, name))
            if (
                receipt is None
                or receipt.get("schema_version") != RUN_RECEIPT_SCHEMA_VERSION
                or receipt.get("token") != token
                or receipt.get("status") not in {"submitting", "unknown"}
            ):
                continue
            digest = receipt.get("snapshot_hash")
            if isinstance(digest, str) and _TOKEN_RE.fullmatch(digest):
                references.setdefault(digest, set()).add("submission:" + token)
    return references


def _snapshot_created_epoch(created_at):
    try:
        parsed = time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return float(calendar.timegm(parsed))


def _directory_size(path):
    total = 0
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(root, name))]
        for name in files:
            candidate = os.path.join(root, name)
            if os.path.islink(candidate):
                continue
            try:
                total += os.path.getsize(candidate)
            except OSError:
                continue
    return total


def _snapshot_inventory(base, now):
    references = _snapshot_references(base)
    snapshots_root = os.path.join(base, "snapshots")
    if not os.path.isdir(snapshots_root):
        return []
    inventory = []
    for digest in sorted(os.listdir(snapshots_root)):
        if not _TOKEN_RE.fullmatch(digest):
            continue
        path = os.path.join(snapshots_root, digest)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        marker = _read_json_object(os.path.join(path, ".complete.json"))
        if marker is None or marker.get("schema_version") != 1 or marker.get("hash") != digest:
            continue
        created_at = marker.get("created_at")
        if not isinstance(created_at, str):
            continue
        created_epoch = _snapshot_created_epoch(created_at)
        stored_size = marker.get("size_bytes")
        size_bytes = (
            stored_size
            if isinstance(stored_size, int) and not isinstance(stored_size, bool) and stored_size >= 0
            else _directory_size(path)
        )
        inventory.append(
            {
                "hash": digest,
                "path": path,
                "size_bytes": size_bytes,
                "created_at": created_at,
                "age_seconds": max(0.0, now - created_epoch) if created_epoch is not None else None,
                "references": sorted(references.get(digest, set())),
            }
        )
    return inventory


def _validate_run_snapshot(base, args):
    if not _TOKEN_RE.fullmatch(args.snapshot_hash):
        raise ValueError("run snapshot hash must be 64 lowercase hexadecimal characters")
    manifest_path = os.path.join(base, "runs", args.run_id, RUN_MANIFEST_FILE)
    manifest = _read_json_object(manifest_path)
    if (
        manifest is None
        or manifest.get("schema_version") != 1
        or manifest.get("run_id") != args.run_id
        or manifest.get("snapshot_hash") != args.snapshot_hash
    ):
        raise ValueError("uploaded run manifest does not reference the planned snapshot")
    marker_path = os.path.join(base, "snapshots", args.snapshot_hash, ".complete.json")
    marker = _read_json_object(marker_path)
    if marker is None or marker.get("schema_version") != 1 or marker.get("hash") != args.snapshot_hash:
        raise ValueError("planned snapshot is not committed on the remote")
    binding = manifest.get("env_binding")
    dependency_job_id = getattr(args, "dependency_job_id", "")
    invalid_dependency_policy = getattr(args, "invalid_dependency_policy", "")
    if dependency_job_id:
        if not dependency_job_id.isdigit():
            raise ValueError("environment dependency job id must be numeric")
        if invalid_dependency_policy not in {"per_job", "site_wide"}:
            raise ValueError("afterok requires an invalid-dependency termination policy")
        if (
            not isinstance(binding, dict)
            or binding.get("wait_policy") != "afterok"
            or binding.get("build_job_id") != dependency_job_id
        ):
            raise ValueError("uploaded run manifest does not match the environment dependency")
    elif invalid_dependency_policy:
        raise ValueError("invalid-dependency policy requires an environment dependency job")


def cmd_snapshot_list(args):
    base = os.path.realpath(os.path.abspath(os.path.expanduser(args.base)))
    _emit(
        {
            "kind": SNAPSHOT_LIFECYCLE_KIND,
            "schema_version": 1,
            "operation": "list",
            "snapshots": _snapshot_inventory(base, time.time()),
        }
    )
    return 0


def cmd_snapshot_gc(args):
    base = os.path.realpath(os.path.abspath(os.path.expanduser(args.base)))
    lock_path = os.path.join(base, "locks", "snapshot-gc.lock")
    minimum_age = max(86400.0, args.min_age)
    with _with_submission_lock(lock_path):
        inventory = _snapshot_inventory(base, time.time())
        candidates = [
            item
            for item in inventory
            if not item["references"] and item["age_seconds"] is not None and item["age_seconds"] >= minimum_age
        ]
        deleted = []
        failed = []
        if args.delete:
            snapshots_root = os.path.realpath(os.path.join(base, "snapshots"))
            for item in candidates:
                path = os.path.realpath(item["path"])
                if os.path.dirname(path) != snapshots_root or not os.path.isdir(path):
                    failed.append(item["hash"])
                    continue
                try:
                    shutil.rmtree(path)
                except OSError:
                    failed.append(item["hash"])
                else:
                    deleted.append(item["hash"])
        _emit(
            {
                "kind": SNAPSHOT_LIFECYCLE_KIND,
                "schema_version": 1,
                "operation": "gc",
                "dry_run": not args.delete,
                "candidates": [item["hash"] for item in candidates],
                "deleted": deleted,
                "failed": failed,
            }
        )
    return 0


def cmd_clean_run(args):
    removed_run = False
    removed_receipt = False
    try:
        base = os.path.realpath(os.path.abspath(os.path.expanduser(args.base)))
        if not _NAME_RE.fullmatch(args.run_id):
            raise ValueError("invalid run id")
        run_dir = os.path.join(base, "runs", args.run_id)

        def remove_paths():
            nonlocal removed_run, removed_receipt
            receipt_path = ""
            receipt = None
            if args.token:
                receipt_path, _lock_path = _submission_paths(base, args.token)
                receipt, receipt_error = _read_receipt(receipt_path, args.token, args.run_id)
                if receipt_error:
                    raise ValueError(receipt_error)
            if os.path.lexists(run_dir):
                if os.path.islink(run_dir) or not os.path.isdir(run_dir):
                    raise ValueError("remote run path is not an owned directory")
                shutil.rmtree(run_dir)
                removed_run = True
            if receipt is not None:
                os.unlink(receipt_path)
                removed_receipt = True
            return removed_run, removed_receipt

        if args.token:
            if not _TOKEN_RE.fullmatch(args.token):
                raise ValueError("invalid submission token")
            _receipt_path, lock_path = _submission_paths(base, args.token)
            with _with_submission_lock(lock_path):
                removed_run, removed_receipt = remove_paths()
        else:
            removed_run, removed_receipt = remove_paths()
        _emit(
            {
                "kind": RUN_CLEAN_KIND,
                "schema_version": 1,
                "ok": True,
                "run_id": args.run_id,
                "token": args.token,
                "removed_run": removed_run,
                "removed_receipt": removed_receipt,
                "error": "",
            }
        )
    except (OSError, ValueError) as exc:
        _emit(
            {
                "kind": RUN_CLEAN_KIND,
                "schema_version": 1,
                "ok": False,
                "run_id": getattr(args, "run_id", ""),
                "token": getattr(args, "token", ""),
                "removed_run": removed_run,
                "removed_receipt": removed_receipt,
                "error": str(exc),
            }
        )
    return 0


def cmd_submit_run(args):
    sbatch_may_have_run = False
    try:
        dependency_job_id = getattr(args, "dependency_job_id", "")
        invalid_dependency_policy = getattr(args, "invalid_dependency_policy", "")
        base = _validate_submission_identity(args.base, args.run_id, args.token, args.job_name)
        expected_script = os.path.realpath(os.path.join(base, "runs", args.run_id, "submit.sbatch"))
        script = os.path.realpath(os.path.abspath(os.path.expanduser(args.script)))
        if script != expected_script or not os.path.isfile(script):
            raise ValueError("submission script must be the run's uploaded submit.sbatch")
        receipt_path, lock_path = _submission_paths(base, args.token)
        snapshot_lock_path = os.path.join(base, "locks", "snapshot-gc.lock")
        with _with_submission_lock(lock_path), _with_submission_lock(snapshot_lock_path):
            receipt, receipt_error = _read_receipt(receipt_path, args.token, args.run_id)
            if receipt is not None:
                status = receipt.get("status", "unknown")
                if status == "submitting":
                    status = "unknown"
                _emit_submission(receipt, "receipt", status=status)
                return 0
            if receipt_error:
                fallback = _receipt_record(args, base, status="unknown", error=receipt_error)
                _emit_submission(fallback, "receipt", status="unknown")
                return 0

            _validate_run_snapshot(base, args)
            if dependency_job_id and invalid_dependency_policy == "per_job":
                help_result = subprocess.run(
                    ["sbatch", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if help_result.returncode != 0 or "--kill-on-invalid-dep" not in (
                    help_result.stdout + help_result.stderr
                ):
                    raise ValueError("sbatch does not support per-job invalid-dependency termination")
            receipt = _receipt_record(args, base, status="submitting")
            _atomic_write_json(receipt_path, receipt)
            command = [
                "sbatch",
                "--parsable",
                "--comment=" + args.token,
                "--job-name=" + args.job_name,
            ]
            if dependency_job_id:
                command.append("--dependency=afterok:" + dependency_job_id)
                if invalid_dependency_policy == "per_job":
                    command.append("--kill-on-invalid-dep=yes")
            command.append(script)
            try:
                sbatch_may_have_run = True
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                receipt.update(
                    status="unknown",
                    updated_at=_utc_now(),
                    error=f"sbatch timed out after {exc.timeout} seconds",
                    stdout=_process_text(exc.stdout),
                    stderr=_process_text(exc.stderr),
                )
            except OSError as exc:
                receipt.update(status="failed", updated_at=_utc_now(), error=f"could not start sbatch: {exc}")
            else:
                receipt.update(stdout=result.stdout, stderr=result.stderr, returncode=result.returncode)
                if result.returncode != 0:
                    detail = result.stderr.strip() or f"sbatch exited with code {result.returncode}"
                    receipt.update(status="failed", updated_at=_utc_now(), error=detail)
                else:
                    job_id = _parse_sbatch_job_id(result.stdout)
                    if job_id:
                        receipt.update(status="submitted", job_id=job_id, updated_at=_utc_now(), error="")
                    else:
                        receipt.update(
                            status="unknown",
                            updated_at=_utc_now(),
                            error="sbatch succeeded but returned no parsable job id",
                        )
            _atomic_write_json(receipt_path, receipt)
            _emit_submission(receipt, "sbatch")
            return 0
    except (OSError, ValueError) as exc:
        status = "unknown" if sbatch_may_have_run else "failed"
        fallback = {
            "token": getattr(args, "token", ""),
            "run_id": getattr(args, "run_id", ""),
            "job_name": getattr(args, "job_name", ""),
            "job_id": "",
            "status": status,
            "error": str(exc),
        }
        _emit_submission(fallback, "helper", status=status)
        return 0


def _scheduler_rows(output):
    rows = []
    for line in output.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        match = re.match(r"^([0-9]+)", parts[0].strip())
        if match:
            rows.append((match.group(1), parts[1].strip(), parts[2].strip()))
    return rows


def _unique_match(rows, predicate):
    matches = {job_id for job_id, comment, name in rows if predicate(comment, name)}
    if len(matches) == 1:
        return next(iter(matches)), ""
    if len(matches) > 1:
        return "", "multiple Slurm jobs matched the submission token"
    return "", ""


def cmd_reconcile_run(args):
    try:
        base = _validate_submission_identity(args.base, args.run_id, args.token, args.job_name)
        receipt_path, lock_path = _submission_paths(base, args.token)
        with _with_submission_lock(lock_path):
            receipt, receipt_error = _read_receipt(receipt_path, args.token, args.run_id)
            if receipt is not None and receipt.get("status") in {"submitted", "failed"}:
                _emit_submission(receipt, "receipt")
                return 0
            if receipt is None:
                receipt = _receipt_record(args, base, status="unknown", error=receipt_error)

            rows = []
            errors = []
            queue = _slurm_query(
                ["squeue", "-h", "-o", "%i|%k|%j"],
                SCAN_KIND_SQUEUE,
                timeout=60,
            )
            if queue["returncode"] == 0:
                rows.extend(_scheduler_rows(queue["output"]))
            else:
                errors.append(queue["error"] or queue["stderr"] or "squeue query failed")
            job_id, match_error = _unique_match(rows, lambda comment, _name: comment == args.token)
            source = "comment"
            if not job_id and not match_error:
                accounting = _slurm_query(
                    ["sacct", "-n", "-X", "-P", "--format=JobIDRaw,Comment,JobName"],
                    SCAN_KIND_SACCT,
                    timeout=60,
                )
                if accounting["returncode"] == 0:
                    rows.extend(_scheduler_rows(accounting["output"]))
                else:
                    errors.append(accounting["error"] or accounting["stderr"] or "sacct query failed")
                job_id, match_error = _unique_match(rows, lambda comment, _name: comment == args.token)
            if not job_id and not match_error:
                suffix = args.token[-12:]
                job_id, match_error = _unique_match(rows, lambda _comment, name: name.endswith(suffix))
                source = "job_name"

            if job_id:
                receipt.update(status="submitted", job_id=job_id, updated_at=_utc_now(), error="")
                _atomic_write_json(receipt_path, receipt)
                _emit_submission(receipt, source)
            else:
                detail = match_error or "; ".join(errors) or "no matching Slurm job found"
                receipt.update(status="unknown", updated_at=_utc_now(), error=detail)
                _atomic_write_json(receipt_path, receipt)
                _emit_submission(receipt, "ambiguous" if match_error else "not_found")
            return 0
    except (OSError, ValueError) as exc:
        fallback = {
            "token": getattr(args, "token", ""),
            "run_id": getattr(args, "run_id", ""),
            "job_name": getattr(args, "job_name", ""),
            "job_id": "",
            "status": "unknown",
            "error": str(exc),
        }
        _emit_submission(fallback, "helper", status="unknown")
        return 0


def _slurm_query(argv, source, timeout=120, invalid_job_is_empty=False):
    """Return enough query metadata to distinguish an empty result from failure."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {
            "source": source,
            "observed_at": time.time(),
            "returncode": None,
            "stderr": _process_text(exc.stderr),
            "error": f"{source} query timed out after {exc.timeout} seconds",
            "output": _process_text(exc.stdout),
        }
    except OSError as exc:
        return {
            "source": source,
            "observed_at": time.time(),
            "returncode": None,
            "stderr": "",
            "error": f"{source} query could not start: {exc}",
            "output": "",
        }
    if (
        invalid_job_is_empty
        and proc.returncode != 0
        and not proc.stdout.strip()
        and "Invalid job id specified" in proc.stderr
    ):
        return {
            "source": source,
            "observed_at": time.time(),
            "returncode": 0,
            "stderr": "",
            "error": "",
            "output": "",
        }
    return {
        "source": source,
        "observed_at": time.time(),
        "returncode": proc.returncode,
        "stderr": proc.stderr,
        "error": "" if proc.returncode == 0 else f"{source} query exited with code {proc.returncode}",
        "output": proc.stdout,
    }


def _environment_dependency(base, run_id):
    manifest = _read_json_object(os.path.join(base, "runs", run_id, RUN_MANIFEST_FILE))
    if manifest is None:
        return None
    binding = manifest.get("env_binding")
    if not isinstance(binding, dict) or binding.get("wait_policy") != "afterok":
        return None
    env_id = binding.get("env_id")
    attempt_id = binding.get("attempt_id")
    generation_id = binding.get("generation_id")
    if not isinstance(env_id, str) or not isinstance(attempt_id, str) or not isinstance(generation_id, str):
        return {
            "kind": SCAN_KIND_ENV_DEPENDENCY,
            "run_id": run_id,
            "state": "unknown",
            "reason": "run manifest has an invalid environment binding",
        }
    record = _read_json_object(os.path.join(base, "envs", "registry", env_id + ".json"))
    if record is None:
        state = "unknown"
        reason = "environment registry record is missing"
    else:
        generation = next(
            (item for item in record.get("generations") or [] if item.get("generation_id") == generation_id),
            None,
        )
        attempt = next(
            (item for item in record.get("attempts") or [] if item.get("attempt_id") == attempt_id),
            None,
        )
        if generation is not None and generation.get("status") == "READY":
            state = "ready"
            reason = "bound environment generation is READY"
        elif attempt is not None and attempt.get("status") == "FAILED":
            state = "ENV_BUILD_FAILED"
            reason = attempt.get("error_summary") or "environment build failed"
        elif attempt is not None and attempt.get("status") == "CANCELLED":
            state = "ENV_BUILD_CANCELLED"
            reason = attempt.get("error_summary") or "environment build was cancelled"
        elif attempt is not None and attempt.get("status") in {
            "STAGING",
            "QUEUED",
            "BUILDING",
            "VERIFYING",
            "BUILD_UNKNOWN",
        }:
            state = "waiting"
            reason = "Waiting for environment {} build {}".format(env_id, binding.get("build_job_id", ""))
        else:
            state = "unknown"
            reason = "environment attempt no longer matches the run binding"
    return {
        "kind": SCAN_KIND_ENV_DEPENDENCY,
        "run_id": run_id,
        "env_id": env_id,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "state": state,
        "reason": reason,
    }


def cmd_scan(args):
    _emit({"kind": SCAN_KIND_HEADER, "agent_version": AGENT_VERSION})
    if args.jobs:
        # scheduler state piggybacks on the scan so one ssh session refreshes everything
        squeue = _slurm_query(
            ["squeue", "-h", "-o", "%i|%T|%R", "-j", args.jobs],
            SCAN_KIND_SQUEUE,
            invalid_job_is_empty=True,
        )
        squeue["kind"] = SCAN_KIND_SQUEUE
        _emit(squeue)
        sacct = _slurm_query(
            ["sacct", "-n", "-P", "--format=JobID,State,ExitCode,Reason", "-j", args.jobs],
            SCAN_KIND_SACCT,
        )
        sacct["kind"] = SCAN_KIND_SACCT
        _emit(sacct)
    since = args.since or 0.0
    for run_id in args.run or []:
        dependency = _environment_dependency(args.base, run_id)
        if dependency is not None:
            _emit(dependency)
        results_root = os.path.join(args.base, "runs", run_id, RESULTS_DIR)
        if not os.path.isdir(results_root):
            continue
        for entry in sorted(os.listdir(results_root)):
            status_path = os.path.join(results_root, entry, STATUS_FILE)
            try:
                mtime = os.path.getmtime(status_path)
            except OSError:
                continue
            if mtime <= since:
                continue
            try:
                with open(status_path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception as exc:
                data = {"task_id": entry, "state": "UNKNOWN", "reason": f"unreadable status.json: {exc}"}
            record = {"kind": SCAN_KIND_TASK, "run_id": run_id, "mtime": mtime}
            record.update(data)
            _emit(record)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="slurmdeck-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--run-root", required=True)
    exec_parser.add_argument("--code-dir", required=True)
    exec_parser.add_argument("--index", type=int, required=True)
    exec_parser.set_defaults(func=cmd_exec)

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--base", required=True)
    scan_parser.add_argument("--run", action="append")
    scan_parser.add_argument("--since", type=float, default=0.0)
    scan_parser.add_argument("--jobs", default="", help="comma-separated Slurm job ids to query")
    scan_parser.set_defaults(func=cmd_scan)

    submit_parser = subparsers.add_parser("submit-run")
    submit_parser.add_argument("--base", required=True)
    submit_parser.add_argument("--run-id", required=True)
    submit_parser.add_argument("--token", required=True)
    submit_parser.add_argument("--job-name", required=True)
    submit_parser.add_argument("--script", required=True)
    submit_parser.add_argument("--snapshot-hash", default="")
    submit_parser.add_argument("--dependency-job-id", default="")
    submit_parser.add_argument("--invalid-dependency-policy", default="")
    submit_parser.add_argument("--timeout", type=float, default=120.0)
    submit_parser.set_defaults(func=cmd_submit_run)

    reconcile_parser = subparsers.add_parser("reconcile-run")
    reconcile_parser.add_argument("--base", required=True)
    reconcile_parser.add_argument("--run-id", required=True)
    reconcile_parser.add_argument("--token", required=True)
    reconcile_parser.add_argument("--job-name", required=True)
    reconcile_parser.set_defaults(func=cmd_reconcile_run)

    snapshot_list_parser = subparsers.add_parser("snapshot-list")
    snapshot_list_parser.add_argument("--base", required=True)
    snapshot_list_parser.set_defaults(func=cmd_snapshot_list)

    snapshot_gc_parser = subparsers.add_parser("snapshot-gc")
    snapshot_gc_parser.add_argument("--base", required=True)
    snapshot_gc_parser.add_argument("--min-age", type=float, default=86400.0)
    snapshot_gc_parser.add_argument("--delete", action="store_true")
    snapshot_gc_parser.set_defaults(func=cmd_snapshot_gc)

    clean_run_parser = subparsers.add_parser("clean-run")
    clean_run_parser.add_argument("--base", required=True)
    clean_run_parser.add_argument("--run-id", required=True)
    clean_run_parser.add_argument("--token", default="")
    clean_run_parser.set_defaults(func=cmd_clean_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
