#!/usr/bin/env python3
"""SlurmDeck environment registry/executor helper (stdlib-only, Python >= 3.8)."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

JSON_PREFIX = "SLURMDECK_JSON\t"
KIND = "slurmdeck.env-registry.v1"
SCHEMA_VERSION = 1

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_STATUSES = {
    "PLANNED",
    "STAGING",
    "QUEUED",
    "BUILDING",
    "VERIFYING",
    "READY",
    "FAILED",
    "CANCELLED",
    "BUILD_UNKNOWN",
    "REMOVING",
    "REMOVED",
    "REMOVE_UNKNOWN",
}
_ACTIVE_BUILD_STATES = {"STAGING", "QUEUED", "BUILDING", "VERIFYING", "BUILD_UNKNOWN"}


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit(operation, *, ok, **values):
    payload = {"kind": KIND, "schema_version": SCHEMA_VERSION, "operation": operation, "ok": ok}
    payload.update(values)
    sys.stdout.write(JSON_PREFIX + json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _base(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _registry_path(base, env_id):
    return os.path.join(base, "envs", "registry", env_id + ".json")


def _lock_path(base, full_hash):
    return os.path.join(base, "locks", "env", full_hash, ".lock")


def _attempt_dir(base, env_id, attempt_id):
    return os.path.join(base, "envs", "attempts", env_id, attempt_id)


def _inbox_dir(base, attempt_id):
    return os.path.join(base, "envs", "inbox", attempt_id)


def _receipt_path(base, attempt_id):
    return os.path.join(base, "receipts", "env", attempt_id + ".json")


@contextlib.contextmanager
def _locked(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _atomic_text(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _validate_record(record):
    if not isinstance(record, dict):
        raise ValueError("registry record must be a JSON object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("registry record has an unsupported schema_version")
    env_id = record.get("env_id")
    full_hash = record.get("full_hash")
    if not isinstance(env_id, str) or not _NAME_RE.fullmatch(env_id):
        raise ValueError("registry record has an invalid env_id")
    if not isinstance(full_hash, str) or not _HASH_RE.fullmatch(full_hash):
        raise ValueError("registry record has an invalid full_hash")
    if not env_id.endswith("-" + full_hash[:12]):
        raise ValueError("registry env_id does not match full_hash")
    backend = record.get("backend")
    ownership = record.get("ownership")
    if backend not in {"conda", "existing"}:
        raise ValueError("registry record has an invalid backend")
    if ownership not in {"managed", "external"}:
        raise ValueError("registry record has an invalid ownership")
    if (backend, ownership) not in {("conda", "managed"), ("existing", "external")}:
        raise ValueError("registry backend and ownership do not match")
    if record.get("status") not in _STATUSES:
        raise ValueError("registry record has an invalid status")
    forbidden = {"reference_count", "references", "desired_by_project"}.intersection(record)
    if forbidden:
        raise ValueError("registry record contains dynamic view fields: " + ", ".join(sorted(forbidden)))
    provenance = record.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("canonical_spec_hash") != full_hash:
        raise ValueError("registry provenance does not match full_hash")
    if not isinstance(record.get("attempts"), list) or not isinstance(record.get("generations"), list):
        raise ValueError("registry attempt and generation collections must be lists")
    return record


def _validate_request(request):
    if not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("build request has an unsupported schema_version")
    for key in (
        "env_id",
        "full_hash",
        "attempt_id",
        "generation_id",
        "prefix",
        "created_at",
        "environment_file_name",
        "isolated_environment_file_name",
        "environment_file_hash",
        "channel_priority",
        "solver",
        "platform",
        "conda_executable",
    ):
        if not isinstance(request.get(key), str) or not request[key]:
            raise ValueError(f"build request has invalid {key}")
    if not _NAME_RE.fullmatch(request["env_id"]):
        raise ValueError("build request has invalid env_id")
    if not _HASH_RE.fullmatch(request["full_hash"]):
        raise ValueError("build request has invalid full_hash")
    if not request["env_id"].endswith("-" + request["full_hash"][:12]):
        raise ValueError("build request env_id does not match full_hash")
    if not _NAME_RE.fullmatch(request["attempt_id"]) or not _NAME_RE.fullmatch(request["generation_id"]):
        raise ValueError("build request has invalid attempt or generation id")
    if not _NAME_RE.fullmatch(request["environment_file_name"]) or not _NAME_RE.fullmatch(
        request["isolated_environment_file_name"]
    ):
        raise ValueError("build request has invalid environment file name")
    if request.get("executor") not in {"slurm", "login"}:
        raise ValueError("build request has invalid executor")
    channels = request.get("channels")
    if not isinstance(channels, list) or not channels or not all(isinstance(item, str) and item for item in channels):
        raise ValueError("build request has invalid channels")
    for key in ("modules", "module_initialization", "post_install"):
        if not isinstance(request.get(key), list) or not all(isinstance(item, str) for item in request[key]):
            raise ValueError(f"build request has invalid {key}")
    if not isinstance(request.get("resolved_resources"), dict):
        raise ValueError("build request has invalid resolved_resources")
    provenance = request.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("canonical_spec_hash") != request["full_hash"]:
        raise ValueError("build request provenance does not match full_hash")
    if provenance.get("environment_file_hash") != request["environment_file_hash"]:
        raise ValueError("build request provenance does not match environment file")
    return request


def _validate_existing_request(request):
    if not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("existing environment request has an unsupported schema_version")
    for key in ("env_id", "full_hash", "prefix", "created_at"):
        if not isinstance(request.get(key), str) or not request[key]:
            raise ValueError(f"existing environment request has invalid {key}")
    if not _NAME_RE.fullmatch(request["env_id"]) or not _HASH_RE.fullmatch(request["full_hash"]):
        raise ValueError("existing environment request has invalid identity")
    if not request["env_id"].endswith("-" + request["full_hash"][:12]):
        raise ValueError("existing environment request env_id does not match full_hash")
    for key in ("modules", "module_initialization"):
        if not isinstance(request.get(key), list) or not all(isinstance(item, str) for item in request[key]):
            raise ValueError(f"existing environment request has invalid {key}")
    provenance = request.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("canonical_spec_hash") != request["full_hash"]:
        raise ValueError("existing environment request provenance does not match full_hash")
    return request


def _load_record(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return _validate_record(json.load(handle)), ""
    except FileNotFoundError:
        return None, ""
    except Exception as exc:
        return None, f"registry record is corrupt: {exc}"


def _load_request(path):
    with open(path, encoding="utf-8") as handle:
        return _validate_request(json.load(handle))


def _find_attempt(record, attempt_id):
    for attempt in record.get("attempts") or []:
        if isinstance(attempt, dict) and attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def _receipt(base, request, state, **values):
    payload = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "env_id": request["env_id"],
        "attempt_id": request["attempt_id"],
        "generation_id": request["generation_id"],
        "state": state,
        "updated_at": _utc_now(),
    }
    payload.update(values)
    _atomic_write(_receipt_path(base, request["attempt_id"]), payload)


def _load_attempt_receipt(base, record, attempt):
    path = _receipt_path(base, attempt["attempt_id"])
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None, ""
    except Exception as exc:
        return None, f"attempt receipt is unreadable: {exc}"
    if not isinstance(payload, dict) or payload.get("kind") != KIND or payload.get("schema_version") != SCHEMA_VERSION:
        return None, "attempt receipt has an invalid contract"
    if (
        payload.get("env_id") != record["env_id"]
        or payload.get("attempt_id") != attempt["attempt_id"]
        or payload.get("generation_id") != attempt.get("generation_id")
    ):
        return None, "attempt receipt identity does not match the registry"
    return payload, ""


def _mark_attempt_failed(record, attempt, code, summary):
    now = _utc_now()
    attempt["status"] = "FAILED"
    attempt["scheduler_state"] = "FAILED"
    attempt["scheduler_reason"] = code
    attempt["ended_at"] = now
    attempt["error_code"] = code
    attempt["error_summary"] = summary
    record["status"] = "FAILED"
    record["updated_at"] = now
    record["current_attempt"] = None
    record["last_error"] = {
        "code": code,
        "summary": summary,
        "detail": "",
        "remediation": "Prepare again; no Slurm job was submitted for the interrupted attempt.",
        "context": {"attempt_id": attempt["attempt_id"]},
    }


def _mark_attempt_unknown(record, attempt, summary):
    now = _utc_now()
    attempt["status"] = "BUILD_UNKNOWN"
    attempt["scheduler_reason"] = "submission outcome is unknown"
    attempt["error_code"] = "BUILD_UNKNOWN"
    attempt["error_summary"] = summary
    record["status"] = "BUILD_UNKNOWN"
    record["updated_at"] = now
    record["last_error"] = {
        "code": "BUILD_UNKNOWN",
        "summary": summary,
        "detail": "",
        "remediation": "Reconcile again; SlurmDeck will not automatically resubmit this attempt.",
        "context": {"attempt_id": attempt["attempt_id"]},
    }


def _reconcile_slurm_receipt(base, record, attempt):
    if (
        attempt.get("executor") != "slurm"
        or attempt.get("status") not in _ACTIVE_BUILD_STATES
        or str(attempt.get("job_id", "")).isdigit()
    ):
        return
    receipt, error = _load_attempt_receipt(base, record, attempt)
    if error:
        _mark_attempt_unknown(record, attempt, error)
        return
    if receipt is None:
        if attempt.get("status") == "STAGING":
            _mark_attempt_failed(
                record,
                attempt,
                "ENV_STAGING_INTERRUPTED",
                "environment staging stopped before its first durable receipt",
            )
        else:
            _mark_attempt_unknown(record, attempt, "environment submission receipt is missing")
        return
    state = receipt.get("state")
    if state in {"staged"}:
        _mark_attempt_failed(
            record,
            attempt,
            "ENV_STAGING_INTERRUPTED",
            "environment staging stopped before sbatch",
        )
    elif state == "submitting":
        _mark_attempt_unknown(record, attempt, "sbatch may have run but no durable job id was recorded")
    elif state == "submitted" and str(receipt.get("job_id", "")).isdigit():
        attempt["status"] = "QUEUED"
        attempt["job_id"] = str(receipt["job_id"])
        attempt["scheduler_state"] = "PENDING"
        attempt["scheduler_reason"] = ""
        record["status"] = "QUEUED"
        record["updated_at"] = _utc_now()
        record["last_error"] = None
    elif state == "failed":
        _mark_attempt_failed(
            record,
            attempt,
            str(receipt.get("error_code") or "ENV_BUILD_FAILED"),
            str(receipt.get("error_summary") or "environment attempt failed"),
        )
    elif state == "cancelled":
        now = _utc_now()
        attempt["status"] = "CANCELLED"
        attempt["scheduler_state"] = "CANCELLED"
        attempt["ended_at"] = now
        attempt["error_code"] = "ENV_BUILD_CANCELLED"
        attempt["error_summary"] = "environment attempt was cancelled"
        record["status"] = "CANCELLED"
        record["updated_at"] = now
        record["current_attempt"] = None
    else:
        _mark_attempt_unknown(record, attempt, "attempt receipt does not contain a trustworthy job id")


def _new_record(request, attempt, status):
    return {
        "schema_version": SCHEMA_VERSION,
        "env_id": request["env_id"],
        "full_hash": request["full_hash"],
        "backend": "conda",
        "ownership": "managed",
        "status": status,
        "active_generation": None,
        "active_prefix": None,
        "created_at": request["created_at"],
        "updated_at": _utc_now(),
        "verified_at": None,
        "current_attempt": request["attempt_id"],
        "generations": [],
        "attempts": [attempt],
        "last_error": None,
        "provenance": request["provenance"],
    }


def _new_attempt(base, request):
    build_dir = _attempt_dir(base, request["env_id"], request["attempt_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": request["attempt_id"],
        "status": "STAGING",
        "executor": request["executor"],
        "generation_id": request["generation_id"],
        "prefix": request["prefix"],
        "job_id": "",
        "scheduler_state": "",
        "scheduler_reason": "",
        "resolved_resources": request["resolved_resources"],
        "module_stack": request["modules"],
        "conda_executable": request["conda_executable"],
        "conda_version": "",
        "resolved_channels": [channel for channel in request["channels"] if channel != "nodefaults"],
        "build_dir": build_dir,
        "stdout_path": os.path.join(build_dir, "build.out"),
        "stderr_path": os.path.join(build_dir, "build.err"),
        "created_at": request["created_at"],
        "started_at": None,
        "ended_at": None,
        "error_code": "",
        "error_summary": "",
        "login_host": "",
        "login_pid": None,
        "heartbeat_at": None,
    }


def _set_failure(base, record, attempt, request, code, summary):
    now = _utc_now()
    attempt["status"] = "FAILED"
    if attempt.get("executor") == "slurm":
        attempt["scheduler_state"] = "FAILED"
        attempt["scheduler_reason"] = code
    attempt["ended_at"] = now
    attempt["error_code"] = code
    attempt["error_summary"] = summary
    record["status"] = "FAILED"
    record["updated_at"] = now
    record["current_attempt"] = None
    record["last_error"] = {
        "code": code,
        "summary": summary,
        "detail": "",
        "remediation": (
            "Accept the channel terms explicitly outside SlurmDeck, then prepare again."
            if code == "CHANNEL_TERMS_REQUIRED"
            else "Inspect the persisted attempt stderr and correct the environment configuration."
        ),
        "context": {"attempt_id": attempt["attempt_id"]},
    }
    _validate_record(record)
    _atomic_write(_registry_path(base, record["env_id"]), record)
    _receipt(base, request, "failed", error_code=code, error_summary=summary)


def _directive(name, value):
    if value is None or value == "":
        return ""
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"resource {name} contains a newline")
    return f"#SBATCH --{name}={text}\n"


def _render_sbatch(base, request, attempt):
    resources = request["resolved_resources"]
    script = [
        "#!/usr/bin/env bash\n",
        "#SBATCH --job-name=sd-env-{}\n".format(request["env_id"]),
        _directive("time", resources.get("time")),
        _directive("cpus-per-task", resources.get("cpus")),
        _directive("mem", resources.get("mem")),
        _directive("gres", resources.get("gres")),
        _directive("partition", resources.get("partition")),
        _directive("account", resources.get("account")),
        _directive("qos", resources.get("qos")),
        _directive("constraint", resources.get("constraint")),
        _directive("output", attempt["stdout_path"]),
        _directive("error", attempt["stderr_path"]),
        "set -euo pipefail\n",
        "{} {} build --base {} --env-id {} --attempt-id {} > /dev/null\n".format(
            shlex.quote(sys.executable),
            shlex.quote(os.path.join(attempt["build_dir"], "env_agent.py")),
            shlex.quote(base),
            shlex.quote(request["env_id"]),
            shlex.quote(request["attempt_id"]),
        ),
    ]
    return "".join(script)


def _render_build(request, attempt):
    conda = shlex.quote(request["conda_executable"])
    prefix = shlex.quote(request["prefix"])
    environment_file = shlex.quote(os.path.join(attempt["build_dir"], request["isolated_environment_file_name"]))
    version_file = shlex.quote(os.path.join(attempt["build_dir"], "conda-version.txt"))
    explicit_file = shlex.quote(os.path.join(attempt["build_dir"], "explicit.txt"))
    lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    lines.extend(request["module_initialization"])
    lines.extend(f"module load {shlex.quote(module)}" for module in request["modules"])
    lines.append(f"{conda} --version > {version_file}")
    command = "{} env create --prefix {} --file {} --yes --solver {}".format(
        conda,
        prefix,
        environment_file,
        shlex.quote(request["solver"]),
    )
    lines.append(command)
    lines.append(f"export CONDA_PREFIX={prefix}")
    lines.append("export PATH={}:$PATH".format(shlex.quote(request["prefix"] + "/bin")))
    activation_dir = shlex.quote(request["prefix"] + "/etc/conda/activate.d")
    lines.extend(
        [
            f"if [ -d {activation_dir} ]; then",
            f"  for _script in {activation_dir}/*.sh; do",
            '    [ -r "$_script" ] && . "$_script"',
            "  done",
            "fi",
        ]
    )
    lines.extend(request["post_install"])
    if request.get("smoke_test"):
        lines.append(request["smoke_test"])
    lines.append(f"{conda} list --explicit --prefix {prefix} > {explicit_file}")
    return "\n".join(lines) + "\n"


def _heartbeat(path):
    _atomic_text(path, _utc_now())


def _run_with_heartbeat(command, *, cwd, env, stdout_path, stderr_path, heartbeat_path):
    with open(stdout_path, "ab") as stdout_handle, open(stderr_path, "ab") as stderr_handle:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout_handle, stderr=stderr_handle)
        while process.poll() is None:
            _heartbeat(heartbeat_path)
            time.sleep(0.1)
        _heartbeat(heartbeat_path)
        return process.returncode


def _tail(path, limit=8000):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _failure_code(stderr):
    lowered = stderr.lower()
    if "terms of service" in lowered or "conda tos accept" in lowered or "channel tos" in lowered:
        return "CHANNEL_TERMS_REQUIRED"
    return "ENV_BUILD_FAILED"


def _explicit_urls(path):
    urls = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if value and not value.startswith(("#", "@")):
                urls.append(value)
    return urls


def _resolved_channel_urls(urls):
    resolved = []
    for url in urls:
        without_hash = url.split("#", 1)[0].rstrip("/")
        parts = without_hash.rsplit("/", 2)
        channel = parts[0] if len(parts) == 3 else without_hash
        if channel not in resolved:
            resolved.append(channel)
    return resolved


def _url_allowed(url, channels):
    for channel in channels:
        if channel == "nodefaults":
            continue
        normalized = channel.rstrip("/")
        if "://" in normalized and url.startswith(normalized + "/"):
            return True
        if normalized == "defaults" and "repo.anaconda.com/pkgs/" in url:
            return True
        if "/" in normalized and ("/{}/".format(normalized.strip("/"))) in url:
            return True
        if f"conda.anaconda.org/{normalized}/" in url:
            return True
    return False


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _run_references(base):
    references = {}  # type: dict[str, list[str]]
    generation_references = {}  # type: dict[str, list[str]]
    runs_root = os.path.join(base, "runs")
    if not os.path.isdir(runs_root):
        return references, generation_references
    for name in sorted(os.listdir(runs_root)):
        if not _NAME_RE.fullmatch(name):
            continue
        manifest_path = os.path.join(runs_root, name, "run.json")
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            if not isinstance(manifest, dict):
                continue
            binding = manifest.get("env_binding")
            if not isinstance(binding, dict):
                continue
            env_id = binding.get("env_id")
            if not isinstance(env_id, str) or not _NAME_RE.fullmatch(env_id):
                continue
            project_id = manifest.get("project_id")
            run_id = manifest.get("run_id")
            if not isinstance(project_id, str) or not project_id:
                project_id = "unknown-project"
            if not isinstance(run_id, str) or not run_id:
                run_id = name
            reference = f"run:{project_id}/{run_id}"
            references.setdefault(env_id, []).append(reference)
            generation_id = binding.get("generation_id")
            if isinstance(generation_id, str) and generation_id:
                generation_references.setdefault(f"{env_id}/{generation_id}", []).append(reference)
        except (OSError, ValueError):
            continue
    return references, generation_references


def _scheduler_scan(job_ids):
    queue = {}  # type: dict[str, dict[str, str]]
    accounting = {}  # type: dict[str, dict[str, str]]
    errors = []  # type: list[str]
    if not job_ids:
        return queue, accounting, errors
    joined = ",".join(sorted(job_ids))
    queued = subprocess.run(
        ["squeue", "-h", "-j", joined, "-o", "%i|%T|%R"],
        capture_output=True,
        text=True,
        check=False,
    )
    if queued.returncode != 0:
        errors.append("squeue failed: " + (queued.stderr.strip() or f"exit {queued.returncode}"))
    else:
        for line in queued.stdout.splitlines():
            fields = line.strip().split("|", 2)
            if len(fields) == 3 and fields[0] in job_ids:
                queue[fields[0]] = {"state": fields[1].strip(), "reason": fields[2].strip()}
    accounted = subprocess.run(
        ["sacct", "-n", "-X", "-j", joined, "--format=JobIDRaw,State,ExitCode", "--parsable2"],
        capture_output=True,
        text=True,
        check=False,
    )
    if accounted.returncode != 0:
        errors.append("sacct failed: " + (accounted.stderr.strip() or f"exit {accounted.returncode}"))
    else:
        for line in accounted.stdout.splitlines():
            fields = line.strip().split("|", 2)
            if len(fields) == 3 and fields[0] in job_ids:
                accounting[fields[0]] = {"state": fields[1].strip(), "exit_code": fields[2].strip()}
    return queue, accounting, errors


def _observed_records(base, records, reconcile_receipts=False):
    observed = json.loads(json.dumps(records))
    job_ids = set()
    for record in observed:
        attempt = _find_attempt(record, record.get("current_attempt"))
        if attempt is not None and reconcile_receipts:
            _reconcile_slurm_receipt(base, record, attempt)
            _validate_record(record)
            attempt = _find_attempt(record, record.get("current_attempt"))
        if attempt and attempt.get("executor") == "slurm" and str(attempt.get("job_id", "")).isdigit():
            job_ids.add(attempt["job_id"])
    queue, accounting, errors = _scheduler_scan(job_ids)
    for record in observed:
        attempt = _find_attempt(record, record.get("current_attempt"))
        if attempt is None:
            continue
        if attempt.get("executor") == "login":
            heartbeat = os.path.join(attempt.get("build_dir", ""), "heartbeat")
            age = max(0.0, time.time() - os.path.getmtime(heartbeat)) if os.path.isfile(heartbeat) else None
            alive = _pid_alive(attempt.get("login_pid"))
            if attempt.get("status") in _ACTIVE_BUILD_STATES and (not alive or age is None or age > 30.0):
                attempt["status"] = "BUILD_UNKNOWN"
                attempt["error_code"] = "BUILD_UNKNOWN"
                attempt["error_summary"] = "login build heartbeat is stale or its process cannot be observed"
                record["status"] = "BUILD_UNKNOWN"
                record["last_error"] = {
                    "code": "BUILD_UNKNOWN",
                    "summary": attempt["error_summary"],
                    "detail": "",
                    "remediation": "Reconcile again; SlurmDeck will not automatically resubmit this attempt.",
                    "context": {"attempt_id": attempt["attempt_id"], "login_pid": attempt.get("login_pid")},
                }
            _validate_record(record)
            continue
        if attempt.get("executor") != "slurm":
            continue
        job_id = attempt.get("job_id")
        if job_id in queue:
            state = queue[job_id]["state"].upper().split("+", 1)[0]
            attempt["scheduler_state"] = state
            attempt["scheduler_reason"] = queue[job_id]["reason"]
            if state in {"RUNNING", "COMPLETING"}:
                attempt["status"] = "BUILDING"
                record["status"] = "BUILDING"
            elif state in {"PENDING", "CONFIGURING", "SUSPENDED"}:
                attempt["status"] = "QUEUED"
                record["status"] = "QUEUED"
        elif job_id in accounting:
            state = accounting[job_id]["state"].upper().split("+", 1)[0].split(" ", 1)[0]
            attempt["scheduler_state"] = state
            attempt["scheduler_reason"] = accounting[job_id]["exit_code"]
            if state == "COMPLETED" and record.get("status") != "READY":
                attempt["status"] = "BUILD_UNKNOWN"
                attempt["error_code"] = "BUILD_UNKNOWN"
                attempt["error_summary"] = "Slurm completed but no promotion receipt was observed"
                record["status"] = "BUILD_UNKNOWN"
            elif state.startswith("CANCELLED"):
                attempt["status"] = "CANCELLED"
                attempt["error_code"] = "ENV_BUILD_CANCELLED"
                attempt["error_summary"] = "Slurm cancelled the environment build"
                record["status"] = "CANCELLED"
            elif state in {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL"}:
                attempt["status"] = "FAILED"
                attempt["error_code"] = "ENV_BUILD_FAILED"
                attempt["error_summary"] = f"Slurm build job ended in {state}"
                record["status"] = "FAILED"
                record["last_error"] = {
                    "code": "ENV_BUILD_FAILED",
                    "summary": attempt["error_summary"],
                    "detail": "",
                    "remediation": "Inspect the persisted build stderr and scheduler reason.",
                    "context": {"job_id": job_id, "scheduler_state": state},
                }
        _validate_record(record)
    for record in observed:
        if record.get("status") != "REMOVING":
            continue
        trash = os.path.join(base, "envs", "trash", record["env_id"])
        original_exists = any(os.path.exists(item.get("prefix", "")) for item in record.get("generations") or [])
        if os.path.exists(trash):
            continue
        if original_exists:
            record["status"] = "REMOVE_UNKNOWN"
        else:
            record["status"] = "REMOVED"
        record["updated_at"] = _utc_now()
        _validate_record(record)
    return observed, errors


def _persist_receipt_reconciliation(base, records):
    reconciled = []
    for original in records:
        candidate = _find_attempt(original, original.get("current_attempt"))
        if (
            candidate is None
            or candidate.get("executor") != "slurm"
            or candidate.get("status") not in _ACTIVE_BUILD_STATES
            or str(candidate.get("job_id", "")).isdigit()
        ):
            reconciled.append(original)
            continue
        with _locked(_lock_path(base, original["full_hash"])):
            current, error = _load_record(_registry_path(base, original["env_id"]))
            if error or current is None:
                reconciled.append(original)
                continue
            before = json.loads(json.dumps(current))
            attempt = _find_attempt(current, current.get("current_attempt"))
            if attempt is not None:
                _reconcile_slurm_receipt(base, current, attempt)
            if current != before:
                _atomic_write(_registry_path(base, current["env_id"]), _validate_record(current))
            reconciled.append(current)
    return reconciled


def _path_size(path):
    if os.path.isfile(path) and not os.path.islink(path):
        return os.path.getsize(path)
    total = 0
    if os.path.isdir(path) and not os.path.islink(path):
        for root, directories, files in os.walk(path, topdown=True, followlinks=False):
            directories[:] = [name for name in directories if not os.path.islink(os.path.join(root, name))]
            for name in files:
                candidate = os.path.join(root, name)
                if not os.path.islink(candidate):
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(candidate)
    return total


def cmd_inspect(args):
    base = _base(args.base)
    registry = os.path.join(base, "envs", "registry")
    records = []
    invalid = []
    if os.path.isdir(registry):
        for name in sorted(os.listdir(registry)):
            if not name.endswith(".json"):
                continue
            env_id = name[:-5]
            record, error = _load_record(os.path.join(registry, name))
            if record is None or record.get("env_id") != env_id:
                invalid.append({"name": name, "error": error or "registry filename does not match env_id"})
            else:
                records.append(record)
    _emit("inspect", ok=True, records=records, invalid=invalid)
    return 0


def cmd_scan(args):
    base = _base(args.base)
    registry = os.path.join(base, "envs", "registry")
    records = []
    invalid = []
    if os.path.isdir(registry):
        for name in sorted(os.listdir(registry)):
            if not name.endswith(".json"):
                continue
            env_id = name[:-5]
            record, error = _load_record(os.path.join(registry, name))
            if record is None or record.get("env_id") != env_id:
                invalid.append({"name": name, "error": error or "registry filename does not match env_id"})
            else:
                records.append(record)
    records = _persist_receipt_reconciliation(base, records)
    observed, scheduler_errors = _observed_records(base, records)
    references, generation_references = _run_references(base)
    _emit(
        "scan",
        ok=True,
        records=observed,
        invalid=invalid,
        references=references,
        generation_references=generation_references,
        scheduler_errors=scheduler_errors,
        observed_at=time.time(),
    )
    return 0


def cmd_candidate_check(args):
    operation = "candidate-check"
    try:
        if not _NAME_RE.fullmatch(args.env_id):
            raise ValueError("candidate has invalid env_id")
        if not _HASH_RE.fullmatch(args.full_hash):
            raise ValueError("candidate has invalid full_hash")
        if not args.env_id.endswith("-" + args.full_hash[:12]):
            raise ValueError("candidate env_id does not match full_hash")
        base = _base(args.base)
        path = _registry_path(base, args.env_id)
        with _locked(_lock_path(base, args.full_hash)):
            record, error = _load_record(path)
            if error:
                _emit(operation, ok=False, error=error)
                return 0
            if record is None:
                _emit(operation, ok=True, action="missing", record=None, scheduler_errors=[], error="")
                return 0
            if record["full_hash"] != args.full_hash:
                _emit(operation, ok=False, error="registry identity conflict")
                return 0
            observed, scheduler_errors = _observed_records(base, [record], reconcile_receipts=True)
            current = observed[0]
            if current != record:
                current["updated_at"] = _utc_now()
                _atomic_write(path, _validate_record(current))
            active_prefix = current.get("active_prefix")
            if current.get("status") == "READY" and active_prefix and os.path.isdir(active_prefix):
                action = "reuse"
            elif current.get("status") in _ACTIVE_BUILD_STATES and current.get("current_attempt"):
                action = "attach"
            else:
                action = "retry"
            _emit(
                operation,
                ok=True,
                action=action,
                record=current,
                scheduler_errors=scheduler_errors,
                error="",
            )
            return 0
    except Exception as exc:
        _emit(operation, ok=False, error=str(exc))
        return 0


def _snapshot_committed(base, digest):
    if not digest:
        return None
    if not _HASH_RE.fullmatch(digest):
        raise ValueError("snapshot hash must be 64 lowercase hexadecimal characters")
    marker = os.path.join(base, "snapshots", digest, ".complete.json")
    try:
        with open(marker, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("schema_version") == 1 and payload.get("hash") == digest


def cmd_binding_check(args):
    operation = "binding-check"
    snapshot_exists = None

    def result(*, ok, **values):
        _emit(operation, ok=ok, snapshot_exists=snapshot_exists, **values)

    try:
        binding = json.loads(args.binding_json)
        if not isinstance(binding, dict):
            raise ValueError("binding must be an object")
        env_id = binding.get("env_id")
        generation_id = binding.get("generation_id")
        prefix = binding.get("prefix")
        attempt_id = binding.get("attempt_id")
        build_job_id = binding.get("build_job_id")
        wait_policy = binding.get("wait_policy")
        if not isinstance(env_id, str) or not _NAME_RE.fullmatch(env_id):
            raise ValueError("binding has invalid env_id")
        if not all(isinstance(value, str) for value in (generation_id, prefix, attempt_id, build_job_id)):
            raise ValueError("binding fields must be strings")
        if wait_policy not in {"ready", "afterok"}:
            raise ValueError("binding has invalid wait_policy")
        base = _base(args.base)
        snapshot_exists = _snapshot_committed(base, args.snapshot_hash)
        record, error = _load_record(_registry_path(base, env_id))
        if error:
            result(ok=False, error=error)
            return 0
        if record is None:
            result(ok=True, state="missing", reason="environment registry record is missing", error="")
            return 0
        if generation_id:
            generation = next(
                (item for item in record.get("generations") or [] if item.get("generation_id") == generation_id),
                None,
            )
            if generation is not None and generation.get("status") == "READY" and generation.get("prefix") == prefix:
                if os.path.isdir(prefix):
                    result(ok=True, state="ready", reason="bound generation is READY", error="")
                else:
                    result(ok=True, state="missing", reason="bound generation prefix is missing", error="")
                return 0
        elif (
            record.get("ownership") == "external"
            and record.get("status") == "READY"
            and record.get("active_prefix") == prefix
        ):
            if os.path.isdir(prefix):
                result(ok=True, state="ready", reason="external prefix is READY", error="")
            else:
                result(ok=True, state="missing", reason="external prefix is missing", error="")
            return 0
        attempt = _find_attempt(record, attempt_id)
        if attempt is not None and attempt.get("generation_id") == generation_id and attempt.get("prefix") == prefix:
            status = attempt.get("status")
            if status == "FAILED":
                result(ok=True, state="failed", reason=attempt.get("error_summary") or "build failed", error="")
                return 0
            if status == "CANCELLED":
                result(
                    ok=True,
                    state="cancelled",
                    reason=attempt.get("error_summary") or "build was cancelled",
                    error="",
                )
                return 0
            if (
                wait_policy == "afterok"
                and attempt.get("executor") == "slurm"
                and attempt.get("job_id") == build_job_id
                and str(build_job_id).isdigit()
                and status in _ACTIVE_BUILD_STATES
            ):
                result(ok=True, state="waiting", reason="bound Slurm build is active", error="")
                return 0
        if record.get("status") == "FAILED":
            state = "failed"
        elif record.get("status") == "CANCELLED":
            state = "cancelled"
        else:
            state = "unknown"
        result(
            ok=True,
            state=state,
            reason="registry no longer matches the exact environment binding",
            error="",
        )
        return 0
    except Exception as exc:
        result(ok=False, error=str(exc))
        return 0


def cmd_prepare(args):
    try:
        record = _validate_record(json.loads(args.record_json))
        base = _base(args.base)
        env_id = record["env_id"]
        full_hash = record["full_hash"]
        path = _registry_path(base, env_id)
        with _locked(_lock_path(base, full_hash)):
            existing, error = _load_record(path)
            if error:
                _emit("prepare", ok=False, action="conflict", error=error)
                return 0
            if existing is not None:
                if existing.get("full_hash") != full_hash:
                    _emit("prepare", ok=False, action="conflict", error="registry identity conflict")
                    return 0
                _emit("prepare", ok=True, action="reuse", record=existing, error="")
                return 0
            _atomic_write(path, record)
            _emit("prepare", ok=True, action="create", record=record, error="")
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _emit("prepare", ok=False, action="failed", error=str(exc))
        return 0


def cmd_verify_existing(args):
    operation = "verify-existing"
    try:
        request = _validate_existing_request(json.loads(args.request_json))
        base = _base(args.base)
        path = _registry_path(base, request["env_id"])
        with _locked(_lock_path(base, request["full_hash"])):
            existing, error = _load_record(path)
            if error:
                _emit(operation, ok=False, error=error)
                return 0
            if existing is not None:
                if existing["full_hash"] != request["full_hash"]:
                    _emit(operation, ok=False, error="registry identity conflict")
                    return 0
                if (
                    existing["status"] == "READY"
                    and existing.get("active_prefix") == request["prefix"]
                    and os.path.isdir(request["prefix"])
                ):
                    _emit(operation, ok=True, action="reuse", record=existing, error="")
                    return 0
            prefix = request["prefix"]
            if not os.path.isdir(prefix):
                _emit(operation, ok=False, error=f"external environment prefix does not exist: {prefix}")
                return 0
            lines = ["set -euo pipefail"]
            lines.extend(request["module_initialization"])
            if request["modules"] and not request["module_initialization"]:
                lines.extend(
                    [
                        "if ! command -v module >/dev/null 2>&1; then",
                        "  for _profile in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh "
                        "/usr/share/lmod/lmod/init/bash; do",
                        '    if [ -r "$_profile" ]; then . "$_profile"; break; fi',
                        "  done",
                        "fi",
                    ]
                )
            lines.extend(f"module load {shlex.quote(module)}" for module in request["modules"])
            quoted = shlex.quote(prefix)
            lines.extend(
                [
                    f"if [ -d {quoted}/conda-meta ]; then",
                    f"  export CONDA_PREFIX={quoted}",
                    "  export PATH={}:$PATH".format(shlex.quote(prefix + "/bin")),
                    f"  if [ -d {quoted}/etc/conda/activate.d ]; then",
                    f"    for _script in {quoted}/etc/conda/activate.d/*.sh; do",
                    '      [ -r "$_script" ] && . "$_script"',
                    "    done",
                    "  fi",
                    f"elif [ -f {quoted}/bin/activate ]; then",
                    f"  . {quoted}/bin/activate",
                    "else",
                    "  echo 'prefix is neither a conda environment nor a venv' >&2",
                    "  exit 127",
                    "fi",
                ]
            )
            if request.get("smoke_test"):
                lines.append(request["smoke_test"])
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-c", "\n".join(lines)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "activation probe failed"
                _emit(operation, ok=False, error=f"external environment activation probe failed: {detail}")
                return 0
            now = _utc_now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "env_id": request["env_id"],
                "full_hash": request["full_hash"],
                "backend": "existing",
                "ownership": "external",
                "status": "READY",
                "active_generation": None,
                "active_prefix": prefix,
                "created_at": request["created_at"],
                "updated_at": now,
                "verified_at": now,
                "current_attempt": None,
                "generations": [],
                "attempts": [],
                "last_error": None,
                "provenance": request["provenance"],
            }
            _atomic_write(path, _validate_record(record))
            _emit(operation, ok=True, action="verify", record=record, error="")
        return 0
    except Exception as exc:
        _emit(operation, ok=False, error=str(exc))
        return 0


def cmd_prepare_build(args):
    operation = "prepare-build"
    try:
        request = _validate_request(json.loads(args.request_json))
        base = _base(args.base)
        expected_prefix = os.path.join(
            base,
            "envs",
            "generations",
            request["env_id"],
            request["generation_id"],
        )
        if os.path.realpath(request["prefix"]) != os.path.realpath(expected_prefix):
            _emit(operation, ok=False, error="generation prefix is outside the managed layout")
            return 0
        path = _registry_path(base, request["env_id"])
        inbox = _inbox_dir(base, request["attempt_id"])
        with _locked(_lock_path(base, request["full_hash"])):
            existing, error = _load_record(path)
            if error:
                _emit(operation, ok=False, error=error)
                return 0
            if existing is not None and existing["full_hash"] != request["full_hash"]:
                _emit(operation, ok=False, error="registry identity conflict")
                return 0
            if existing is not None:
                reconciled, _scheduler_errors = _observed_records(base, [existing], reconcile_receipts=True)
                if reconciled[0] != existing:
                    existing = reconciled[0]
                    _atomic_write(path, _validate_record(existing))
            if existing is not None and existing["status"] == "READY" and not request.get("rebuild"):
                shutil.rmtree(inbox, ignore_errors=True)
                _emit(operation, ok=True, action="reuse", record=existing, error="")
                return 0
            if (
                existing is not None
                and existing.get("current_attempt")
                and existing.get("status") in _ACTIVE_BUILD_STATES
            ):
                shutil.rmtree(inbox, ignore_errors=True)
                _emit(operation, ok=True, action="attach", record=existing, error="")
                return 0
            if not os.path.isdir(inbox):
                _emit(operation, ok=False, error="attempt inbox is missing")
                return 0
            staged_request = _load_request(os.path.join(inbox, "request.json"))
            if staged_request != request:
                _emit(operation, ok=False, error="attempt inbox request does not match invocation")
                return 0
            with open(os.path.join(inbox, request["environment_file_name"]), "rb") as environment_handle:
                observed_hash = hashlib.sha256(environment_handle.read()).hexdigest()
            if observed_hash != request["environment_file_hash"]:
                _emit(operation, ok=False, error="attempt inbox environment file hash does not match request")
                return 0
            destination = _attempt_dir(base, request["env_id"], request["attempt_id"])
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if os.path.exists(destination):
                _emit(operation, ok=False, error="attempt directory already exists")
                return 0
            os.replace(inbox, destination)
            attempt = _new_attempt(base, request)
            action = "rebuild" if existing is not None and existing.get("active_generation") else "create"
            if existing is None:
                record = _new_record(request, attempt, "STAGING")
            else:
                record = existing
                record["attempts"].append(attempt)
                record["current_attempt"] = request["attempt_id"]
                record["status"] = "STAGING"
                record["updated_at"] = _utc_now()
                record["last_error"] = None
            _atomic_write(path, _validate_record(record))
            _receipt(base, request, "staged")
            build_script = os.path.join(destination, "build.sh")
            _atomic_text(build_script, _render_build(request, attempt).rstrip("\n"))
            os.chmod(build_script, 0o700)
            if request["executor"] == "slurm":
                sbatch_script = os.path.join(destination, "build.sbatch")
                _atomic_text(sbatch_script, _render_sbatch(base, request, attempt).rstrip("\n"))
                os.chmod(sbatch_script, 0o700)
                attempt["status"] = "BUILD_UNKNOWN"
                attempt["scheduler_reason"] = "submission response pending"
                record["status"] = "BUILD_UNKNOWN"
                record["updated_at"] = _utc_now()
                _atomic_write(path, _validate_record(record))
                _receipt(base, request, "submitting")
                submitted = subprocess.run(
                    ["sbatch", "--parsable", "--comment=slurmdeck-env:{}".format(request["attempt_id"]), sbatch_script],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                job_id = submitted.stdout.strip().split(";", 1)[0]
                if submitted.returncode != 0 or not job_id.isdigit():
                    summary = submitted.stderr.strip() or "sbatch returned no valid job id"
                    _set_failure(base, record, attempt, request, "ENV_SUBMIT_FAILED", summary)
                else:
                    attempt["status"] = "QUEUED"
                    attempt["job_id"] = job_id
                    attempt["scheduler_state"] = "PENDING"
                    attempt["scheduler_reason"] = ""
                    record["status"] = "QUEUED"
                    record["updated_at"] = _utc_now()
                    _atomic_write(path, _validate_record(record))
                    _receipt(base, request, "submitted", job_id=job_id)
            else:
                heartbeat_path = os.path.join(destination, "heartbeat")
                _heartbeat(heartbeat_path)
                with open(attempt["stderr_path"], "ab") as stderr_handle:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            os.path.join(destination, "env_agent.py"),
                            "build",
                            "--base",
                            base,
                            "--env-id",
                            request["env_id"],
                            "--attempt-id",
                            request["attempt_id"],
                        ],
                        cwd=destination,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_handle,
                        start_new_session=True,
                        close_fds=True,
                    )
                attempt["status"] = "BUILDING"
                attempt["started_at"] = _utc_now()
                attempt["login_host"] = socket.gethostname()
                attempt["login_pid"] = process.pid
                attempt["heartbeat_at"] = _utc_now()
                record["status"] = "BUILDING"
                record["updated_at"] = _utc_now()
                _atomic_write(path, _validate_record(record))
                _receipt(base, request, "started", login_pid=process.pid, login_host=attempt["login_host"])
            record, error = _load_record(path)
            if error or record is None:
                _emit(operation, ok=False, error=error or "registry record disappeared")
            else:
                _emit(operation, ok=True, action=action, record=record, error="")
        return 0
    except Exception as exc:
        _emit(operation, ok=False, error=str(exc))
        return 0


def cmd_build(args):
    operation = "build"
    try:
        base = _base(args.base)
        path = _registry_path(base, args.env_id)
        record, error = _load_record(path)
        if error or record is None:
            _emit(operation, ok=False, error=error or "environment not found")
            return 1
        attempt = _find_attempt(record, args.attempt_id)
        if attempt is None:
            _emit(operation, ok=False, error="attempt not found")
            return 1
        request = _load_request(os.path.join(attempt["build_dir"], "request.json"))
        lock = _lock_path(base, record["full_hash"])
        with _locked(lock):
            record, error = _load_record(path)
            if error or record is None:
                _emit(operation, ok=False, error=error or "environment not found")
                return 1
            attempt = _find_attempt(record, args.attempt_id)
            if attempt is None:
                _emit(operation, ok=False, error="attempt not found")
                return 1
            if attempt["status"] == "READY":
                _emit(operation, ok=True, action="built", record=record, error="")
                return 0
            if os.path.exists(request["prefix"]):
                _set_failure(
                    base,
                    record,
                    attempt,
                    request,
                    "GENERATION_PREFIX_EXISTS",
                    "unpublished generation prefix already exists",
                )
                _emit(operation, ok=True, action="failed", record=record, error="")
                return 1
            now = _utc_now()
            attempt["status"] = "BUILDING"
            if attempt.get("executor") == "slurm":
                attempt["scheduler_state"] = "RUNNING"
                attempt["scheduler_reason"] = ""
            attempt["started_at"] = attempt.get("started_at") or now
            attempt["heartbeat_at"] = now
            record["status"] = "BUILDING"
            record["updated_at"] = now
            _atomic_write(path, _validate_record(record))
            _receipt(base, request, "building")
        build_dir = attempt["build_dir"]
        heartbeat_path = os.path.join(build_dir, "heartbeat")
        home = os.path.join(build_dir, "home")
        xdg = os.path.join(home, ".config")
        packages = os.path.join(build_dir, "pkgs")
        os.makedirs(xdg, exist_ok=True)
        os.makedirs(packages, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "CONDARC": os.path.join(build_dir, ".condarc"),
                "HOME": home,
                "XDG_CONFIG_HOME": xdg,
                "CONDA_PKGS_DIRS": packages,
                "CONDA_SUBDIR": request["platform"],
                "CONDA_PLUGINS_AUTO_ACCEPT_TOS": "false",
                "PYTHONNOUSERSITE": "1",
            }
        )
        declared_channels = [channel for channel in request["channels"] if channel != "nodefaults"]
        if "defaults" not in declared_channels:
            # Environment variables have the final precedence over a shared
            # installation's root-prefix configuration.  Keep the symbolic
            # ``defaults`` multichannel inside the explicit allowlist.
            environment["CONDA_DEFAULT_CHANNELS"] = ",".join(declared_channels)
        else:
            environment.pop("CONDA_DEFAULT_CHANNELS", None)
        # conda-anaconda-tos treats CI=true as consent to auto-accept.  A
        # managed SlurmDeck build must never inherit that policy accidentally.
        environment.pop("CI", None)
        returncode = _run_with_heartbeat(
            ["/bin/bash", os.path.join(build_dir, "build.sh")],
            cwd=build_dir,
            env=environment,
            stdout_path=attempt["stdout_path"],
            stderr_path=attempt["stderr_path"],
            heartbeat_path=heartbeat_path,
        )
        with _locked(lock):
            record, error = _load_record(path)
            if error or record is None:
                _emit(operation, ok=False, error=error or "environment not found after build")
                return 1
            attempt = _find_attempt(record, args.attempt_id)
            if attempt is None:
                _emit(operation, ok=False, error="attempt disappeared after build")
                return 1
            if record.get("current_attempt") != args.attempt_id or attempt.get("status") == "CANCELLED":
                _receipt(base, request, "cancelled")
                _emit(operation, ok=True, action="cancelled", record=record, error="")
                return 1
            if returncode != 0:
                stderr = _tail(attempt["stderr_path"])
                code = _failure_code(stderr)
                summary = stderr.strip().splitlines()[-1] if stderr.strip() else "environment build failed"
                _set_failure(base, record, attempt, request, code, summary)
                record, _ = _load_record(path)
                _emit(operation, ok=True, action="failed", record=record, error="")
                return 1
            attempt["status"] = "VERIFYING"
            record["status"] = "VERIFYING"
            record["updated_at"] = _utc_now()
            _atomic_write(path, _validate_record(record))
        urls = _explicit_urls(os.path.join(build_dir, "explicit.txt"))
        invalid_urls = [url for url in urls if not _url_allowed(url, request["channels"])]
        with _locked(lock):
            record, error = _load_record(path)
            if error or record is None:
                _emit(operation, ok=False, error=error or "environment not found during verification")
                return 1
            attempt = _find_attempt(record, args.attempt_id)
            if attempt is None:
                _emit(operation, ok=False, error="attempt disappeared during verification")
                return 1
            if record.get("current_attempt") != args.attempt_id or attempt.get("status") == "CANCELLED":
                _receipt(base, request, "cancelled")
                _emit(operation, ok=True, action="cancelled", record=record, error="")
                return 1
            if invalid_urls:
                _set_failure(
                    base,
                    record,
                    attempt,
                    request,
                    "CHANNEL_ISOLATION_FAILED",
                    f"package URL is outside declared channels: {invalid_urls[0]}",
                )
                record, _ = _load_record(path)
                _emit(operation, ok=True, action="failed", record=record, error="")
                return 1
            now = _utc_now()
            provenance = dict(request["provenance"])
            provenance["package_urls"] = urls
            generation = {
                "schema_version": SCHEMA_VERSION,
                "generation_id": request["generation_id"],
                "attempt_id": request["attempt_id"],
                "prefix": request["prefix"],
                "status": "READY",
                "created_at": request["created_at"],
                "verified_at": now,
                "provenance": provenance,
            }
            if any(item.get("generation_id") == request["generation_id"] for item in record["generations"]):
                _emit(operation, ok=False, error="generation id already exists")
                return 1
            record["generations"].append(generation)
            attempt["status"] = "READY"
            if attempt.get("executor") == "slurm":
                attempt["scheduler_state"] = "COMPLETED"
                attempt["scheduler_reason"] = ""
            attempt["ended_at"] = now
            attempt["heartbeat_at"] = now
            attempt["conda_version"] = _tail(os.path.join(build_dir, "conda-version.txt"), 1000).strip()
            attempt["resolved_channels"] = _resolved_channel_urls(urls)
            record["status"] = "READY"
            record["active_generation"] = request["generation_id"]
            record["active_prefix"] = request["prefix"]
            record["verified_at"] = now
            record["updated_at"] = now
            record["current_attempt"] = None
            record["last_error"] = None
            record["provenance"] = provenance
            _atomic_write(path, _validate_record(record))
            _receipt(base, request, "completed", package_urls=urls)
            _emit(operation, ok=True, action="built", record=record, error="")
        return 0
    except Exception as exc:
        _emit(operation, ok=False, error=str(exc))
        return 1


def cmd_reconcile(args):
    operation = "reconcile"
    try:
        base = _base(args.base)
        path = _registry_path(base, args.env_id)
        record, error = _load_record(path)
        if error or record is None:
            _emit(operation, ok=False, error=error or "environment not found")
            return 0
        with _locked(_lock_path(base, record["full_hash"])):
            record, error = _load_record(path)
            if error or record is None:
                _emit(operation, ok=False, error=error or "environment not found")
                return 0
            attempt = _find_attempt(record, record.get("current_attempt"))
            if attempt is not None and attempt.get("executor") == "login":
                heartbeat = os.path.join(attempt["build_dir"], "heartbeat")
                age = None
                if os.path.isfile(heartbeat):
                    age = max(0.0, time.time() - os.path.getmtime(heartbeat))
                    with contextlib.suppress(OSError):
                        attempt["heartbeat_at"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(heartbeat))
                        )
                alive = _pid_alive(attempt.get("login_pid"))
                stale = age is None or age > args.heartbeat_timeout
                if attempt.get("status") in _ACTIVE_BUILD_STATES and (not alive or stale):
                    attempt["status"] = "BUILD_UNKNOWN"
                    attempt["error_code"] = "BUILD_UNKNOWN"
                    attempt["error_summary"] = "login build heartbeat is stale or its process cannot be observed"
                    record["status"] = "BUILD_UNKNOWN"
                    record["updated_at"] = _utc_now()
                    record["last_error"] = {
                        "code": "BUILD_UNKNOWN",
                        "summary": attempt["error_summary"],
                        "detail": "",
                        "remediation": "Reconcile again; SlurmDeck will not automatically resubmit this attempt.",
                        "context": {"attempt_id": attempt["attempt_id"], "login_pid": attempt.get("login_pid")},
                    }
                    _atomic_write(path, _validate_record(record))
            elif attempt is not None and attempt.get("executor") == "slurm":
                observed, _scheduler_errors = _observed_records(base, [record], reconcile_receipts=True)
                if observed[0] != record:
                    record = observed[0]
                    record["updated_at"] = _utc_now()
                    _atomic_write(path, _validate_record(record))
            _emit(operation, ok=True, action="reconciled", record=record, error="")
        return 0
    except Exception as exc:
        _emit(operation, ok=False, error=str(exc))
        return 0


def cmd_cancel(args):
    base = _base(args.base)
    path = _registry_path(base, args.env_id)
    record, error = _load_record(path)
    if error or record is None:
        _emit("cancel", ok=False, error=error or "environment not found")
        return 0
    with _locked(_lock_path(base, record["full_hash"])):
        record, error = _load_record(path)
        if error or record is None:
            _emit("cancel", ok=False, error=error or "environment not found")
            return 0
        attempt = _find_attempt(record, record.get("current_attempt"))
        if attempt is None:
            _emit("cancel", ok=False, error="environment has no active attempt")
            return 0
        if attempt.get("executor") == "login" and _pid_alive(attempt.get("login_pid")):
            with contextlib.suppress(OSError):
                os.kill(attempt["login_pid"], 15)
        elif attempt.get("job_id"):
            subprocess.run(["scancel", attempt["job_id"]], capture_output=True, check=False)
        attempt["status"] = "CANCELLED"
        if attempt.get("executor") == "slurm":
            attempt["scheduler_state"] = "CANCELLED"
            attempt["scheduler_reason"] = "cancelled by user"
        attempt["ended_at"] = _utc_now()
        record["status"] = "CANCELLED"
        record["updated_at"] = _utc_now()
        record["current_attempt"] = None
        _atomic_write(path, _validate_record(record))
        _emit("cancel", ok=True, record=record, error="")
    return 0


def cmd_remove(args):
    base = _base(args.base)
    path = _registry_path(base, args.env_id)
    record, error = _load_record(path)
    if error or record is None:
        _emit("remove", ok=False, error=error or "environment not found")
        return 0
    references, _generation_references = _run_references(base)
    record_references = references.get(args.env_id, [])
    if record_references and not args.force:
        _emit(
            "remove",
            ok=False,
            error="environment is referenced by " + ", ".join(record_references),
            references=record_references,
        )
        return 0
    with _locked(_lock_path(base, record["full_hash"])):
        record, error = _load_record(path)
        if error or record is None:
            _emit("remove", ok=False, error=error or "environment not found")
            return 0
        if record.get("current_attempt"):
            _emit("remove", ok=False, error="environment has an active build attempt; cancel it first")
            return 0
        if record.get("ownership") == "external":
            removed = json.loads(json.dumps(record))
            removed["status"] = "REMOVED"
            removed["updated_at"] = _utc_now()
            os.unlink(path)
            _emit(
                "remove",
                ok=True,
                action="unregistered",
                record=removed,
                references=record_references,
                external_unregistered=True,
                trash_path="",
                error="",
            )
            return 0
        record["status"] = "REMOVING"
        record["updated_at"] = _utc_now()
        record["last_error"] = None
        _atomic_write(path, _validate_record(record))
        trash_root = os.path.join(base, "envs", "trash", args.env_id)
        if os.path.exists(trash_root):
            record["status"] = "REMOVE_UNKNOWN"
            record["last_error"] = {
                "code": "REMOVE_UNKNOWN",
                "summary": "environment trash destination already exists",
                "detail": "",
                "remediation": "Inspect the trash path before retrying removal.",
                "context": {"trash_path": trash_root},
            }
            _atomic_write(path, _validate_record(record))
            _emit("remove", ok=False, error="environment trash destination already exists")
            return 0
        os.makedirs(trash_root, exist_ok=False)
        try:
            generation_root = os.path.realpath(os.path.join(base, "envs", "generations", args.env_id))
            for generation in record.get("generations") or []:
                prefix = generation.get("prefix", "")
                if not prefix or not os.path.exists(prefix):
                    continue
                if os.path.commonpath([os.path.realpath(prefix), generation_root]) != generation_root:
                    raise ValueError("managed generation prefix is outside the generation layout")
                os.replace(prefix, os.path.join(trash_root, generation["generation_id"]))
        except Exception as exc:
            record["status"] = "REMOVE_UNKNOWN"
            record["updated_at"] = _utc_now()
            record["last_error"] = {
                "code": "REMOVE_UNKNOWN",
                "summary": str(exc),
                "detail": "",
                "remediation": "Inspect generation and trash paths before retrying.",
                "context": {"trash_path": trash_root},
            }
            _atomic_write(path, _validate_record(record))
            _emit("remove", ok=False, error=str(exc), record=record)
            return 0
        subprocess.Popen(
            ["/bin/rm", "-rf", trash_root],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        _emit(
            "remove",
            ok=True,
            action="trashed",
            record=record,
            references=record_references,
            external_unregistered=False,
            trash_path=trash_root,
            error="",
        )
    return 0


def cmd_gc(args):
    base = _base(args.base)
    root = os.path.join(base, "envs", "trash")
    candidates = []
    deleted = []
    failed = []
    if os.path.isdir(root):
        for env_id in sorted(os.listdir(root)):
            env_root = os.path.join(root, env_id)
            if not _NAME_RE.fullmatch(env_id) or not os.path.isdir(env_root) or os.path.islink(env_root):
                continue
            for name in sorted(os.listdir(env_root)):
                path = os.path.join(env_root, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    candidates.append(
                        {
                            "kind": "trash",
                            "path": path,
                            "env_id": env_id,
                            "reason": "environment generation was moved to trash",
                            "size_bytes": _path_size(path),
                        }
                    )
    registry_root = os.path.join(base, "envs", "registry")
    _references, generation_references = _run_references(base)
    if os.path.isdir(registry_root):
        for name in sorted(os.listdir(registry_root)):
            if not name.endswith(".json"):
                continue
            record, error = _load_record(os.path.join(registry_root, name))
            if error or record is None:
                continue
            published_generation_ids = {
                generation.get("generation_id") for generation in record.get("generations") or []
            }
            generation_root = os.path.realpath(os.path.join(base, "envs", "generations", record["env_id"]))
            for attempt in record.get("attempts") or []:
                path = attempt.get("build_dir", "")
                if (
                    attempt.get("status") in {"READY", "FAILED", "CANCELLED"}
                    and attempt.get("attempt_id") != record.get("current_attempt")
                    and path
                    and os.path.isdir(path)
                ):
                    candidates.append(
                        {
                            "kind": "attempt",
                            "path": path,
                            "env_id": record["env_id"],
                            "reason": f"terminal {attempt['status'].lower()} build workspace",
                            "size_bytes": _path_size(path),
                        }
                    )
                generation_id = attempt.get("generation_id", "")
                prefix = attempt.get("prefix", "")
                key = f"{record['env_id']}/{generation_id}"
                if (
                    attempt.get("status") in {"FAILED", "CANCELLED"}
                    and generation_id not in published_generation_ids
                    and not generation_references.get(key)
                    and prefix
                    and os.path.isdir(prefix)
                    and not os.path.islink(prefix)
                    and os.path.commonpath([os.path.realpath(prefix), generation_root]) == generation_root
                ):
                    candidates.append(
                        {
                            "kind": "failed_generation",
                            "path": prefix,
                            "env_id": record["env_id"],
                            "reason": f"unpublished prefix from terminal {attempt['status'].lower()} attempt",
                            "size_bytes": _path_size(prefix),
                        }
                    )
            for generation in record.get("generations") or []:
                key = f"{record['env_id']}/{generation.get('generation_id', '')}"
                path = generation.get("prefix", "")
                if (
                    generation.get("generation_id") != record.get("active_generation")
                    and not generation_references.get(key)
                    and path
                    and os.path.isdir(path)
                ):
                    candidates.append(
                        {
                            "kind": "generation",
                            "path": path,
                            "env_id": record["env_id"],
                            "reason": "inactive and unreferenced generation",
                            "size_bytes": _path_size(path),
                        }
                    )
    inbox_root = os.path.join(base, "envs", "inbox")
    if os.path.isdir(inbox_root):
        for attempt_id in sorted(os.listdir(inbox_root)):
            path = os.path.join(inbox_root, attempt_id)
            if _NAME_RE.fullmatch(attempt_id) and os.path.isdir(path) and not os.path.islink(path):
                candidates.append(
                    {
                        "kind": "inbox",
                        "path": path,
                        "env_id": "",
                        "reason": "orphaned prepare inbox",
                        "size_bytes": _path_size(path),
                    }
                )
    if args.delete:
        for candidate in candidates:
            path = candidate["path"]
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                    deleted.append(path)
            except OSError:
                failed.append(path)
    _emit(
        "gc",
        ok=True,
        dry_run=not args.delete,
        candidates=candidates,
        deleted=deleted,
        failed=failed,
        error="",
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="slurmdeck-env-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--base", required=True)
    inspect_parser.set_defaults(func=cmd_inspect)

    scan_parser = commands.add_parser("scan")
    scan_parser.add_argument("--base", required=True)
    scan_parser.set_defaults(func=cmd_scan)

    candidate_parser = commands.add_parser("candidate-check")
    candidate_parser.add_argument("--base", required=True)
    candidate_parser.add_argument("--env-id", required=True)
    candidate_parser.add_argument("--full-hash", required=True)
    candidate_parser.set_defaults(func=cmd_candidate_check)

    binding_parser = commands.add_parser("binding-check")
    binding_parser.add_argument("--base", required=True)
    binding_parser.add_argument("--binding-json", required=True)
    binding_parser.add_argument("--snapshot-hash", default="")
    binding_parser.set_defaults(func=cmd_binding_check)

    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--base", required=True)
    prepare_parser.add_argument("--record-json", required=True)
    prepare_parser.set_defaults(func=cmd_prepare)

    verify_parser = commands.add_parser("verify-existing")
    verify_parser.add_argument("--base", required=True)
    verify_parser.add_argument("--request-json", required=True)
    verify_parser.set_defaults(func=cmd_verify_existing)

    prepare_build_parser = commands.add_parser("prepare-build")
    prepare_build_parser.add_argument("--base", required=True)
    prepare_build_parser.add_argument("--request-json", required=True)
    prepare_build_parser.set_defaults(func=cmd_prepare_build)

    build_parser = commands.add_parser("build")
    build_parser.add_argument("--base", required=True)
    build_parser.add_argument("--env-id", required=True)
    build_parser.add_argument("--attempt-id", required=True)
    build_parser.set_defaults(func=cmd_build)

    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--base", required=True)
    reconcile_parser.add_argument("--env-id", required=True)
    reconcile_parser.add_argument("--heartbeat-timeout", type=float, default=30.0)
    reconcile_parser.set_defaults(func=cmd_reconcile)

    cancel_parser = commands.add_parser("cancel")
    cancel_parser.add_argument("--base", required=True)
    cancel_parser.add_argument("--env-id", required=True)
    cancel_parser.set_defaults(func=cmd_cancel)

    remove_parser = commands.add_parser("remove")
    remove_parser.add_argument("--base", required=True)
    remove_parser.add_argument("--env-id", required=True)
    remove_parser.add_argument("--force", action="store_true")
    remove_parser.set_defaults(func=cmd_remove)

    gc_parser = commands.add_parser("gc")
    gc_parser.add_argument("--base", required=True)
    gc_parser.add_argument("--delete", action="store_true")
    gc_parser.set_defaults(func=cmd_gc)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
