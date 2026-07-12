"""In-process fake Transport backed by a local directory.

The fake "remote" is a real local directory (the remote's ``resolved_base``
points into it), so path-oriented commands (``test -f``, ``mkdir -p``,
``rm -rf``, ``tail``) operate on real files, and ``exec_python`` genuinely runs
the given script with the local interpreter — the run agent and environment
helper code paths execute for real.

``sbatch --parsable`` can optionally *simulate the cluster*: it synchronously
executes every array task by invoking the uploaded ``agent.py exec`` per
index, which exercises the full submit → execute → status.json pipeline
without SSH or Slurm.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from pathlib import Path

from slurmdeck.transport import ExecResult, StreamHandle, SyncStats, parse_json_lines
from slurmdeck.transport.errors import TransportError

_ENV_OPERATIONS = {
    "inspect",
    "scan",
    "candidate-check",
    "binding-check",
    "prepare",
    "verify-existing",
    "prepare-build",
    "build",
    "reconcile",
    "cancel",
    "remove",
    "gc",
}


class FakeTransport:
    def __init__(self, root: Path, *, simulate_execution: bool = True) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.simulate_execution = simulate_execution
        self.calls: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str, tuple[str, ...]]] = []
        self.connected = False
        #: command-prefix → handler(command) overrides for tests
        self.handlers: dict[str, Callable[[str], ExecResult]] = {}
        #: canned outputs for scheduler queries
        self.squeue_output = ""
        self.sacct_output = ""
        self.squeue_stderr = ""
        self.sacct_stderr = ""
        self.sinfo_output = "short*|01:00:00\n"
        self.sinfo_stderr = ""
        self.squeue_returncode = 0
        self.sacct_returncode = 0
        self.sinfo_returncode = 0
        self.sbatch_returncode = 0
        self.sbatch_stderr = ""
        self.next_job_id = 999001
        self.fail_task_indices: set[int] = set()
        self.env_overrides: dict[str, str] = {}
        self.call_counts: Counter[str] = Counter()
        self._delays: dict[str, float] = {}
        self._scripts: dict[str, deque[object]] = {}
        self._metrics_lock = threading.Lock()

    # -- test controls ----------------------------------------------------------

    def set_delay(self, call_type: str, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake transport delay must be non-negative")
        self._delays[call_type] = seconds

    def script_call(self, call_type: str, *outcomes: object) -> None:
        if not outcomes:
            raise ValueError("script_call requires at least one outcome")
        self._scripts[call_type] = deque(outcomes)

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self.call_counts.clear()
        self.calls.clear()
        self.uploads.clear()
        self.downloads.clear()

    def _prepare_call(self, keys: Sequence[str]) -> object | None:
        with self._metrics_lock:
            for key in keys:
                self.call_counts[key] += 1
            scripted = None
            for key in keys:
                outcomes = self._scripts.get(key)
                if outcomes:
                    scripted = outcomes.popleft()
                    break
            delay = max((self._delays.get(key, 0.0) for key in keys), default=0.0)
        if delay:
            time.sleep(delay)
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted):
            return scripted()
        return scripted

    @staticmethod
    def _python_call_keys(args: Sequence[str]) -> list[str]:
        operation = str(args[0]) if args else "unknown"
        keys = []
        if operation == "submit-run" and "--dependency-job-id" in args:
            keys.append("afterok")
        if operation == "prepare-build" and '"executor":"login"' in "".join(str(value) for value in args):
            keys.append("login")
        if operation in _ENV_OPERATIONS:
            keys.append(f"env:{operation}")
        keys.extend((f"helper:{operation}", "helper", "exec_python"))
        return keys

    # -- Transport protocol -----------------------------------------------------

    def exec(
        self,
        command: str,
        *,
        input_text: str | None = None,
        timeout: float = 60.0,
        check: bool = True,
        retries: int = 0,
    ) -> ExecResult:
        self.calls.append(command)
        scripted = self._prepare_call((f"exec:{command}", "exec"))
        if scripted is not None:
            if not isinstance(scripted, ExecResult):
                raise TypeError("scripted exec outcome must be ExecResult or an exception")
            result = scripted
        else:
            result = None
        if result is None:
            for prefix, handler in self.handlers.items():
                if command.startswith(prefix):
                    result = handler(command)
                    break
            else:
                result = self._default_exec(command)
        if check and result.returncode != 0:
            raise TransportError("fake command failed", command=command, returncode=result.returncode)
        return result

    def _slurm_shims(self) -> Path:
        """PATH dir with Slurm-tool shims (squeue/sacct echo the canned
        outputs), so remote-side scheduler queries and tool probes work
        in-process too."""
        shims = self.root / ".shims"
        shims.mkdir(parents=True, exist_ok=True)
        for name, output, stderr, returncode in (
            ("squeue", self.squeue_output, self.squeue_stderr, self.squeue_returncode),
            ("sacct", self.sacct_output, self.sacct_stderr, self.sacct_returncode),
            ("sinfo", self.sinfo_output, self.sinfo_stderr, self.sinfo_returncode),
        ):
            (shims / f"{name}.out").write_text(output, encoding="utf-8")
            (shims / f"{name}.err").write_text(stderr, encoding="utf-8")
            shim = shims / name
            shim.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "--version" ]; then echo "slurm 23.11.0"; exit 0; fi\n'
                f'cat "{shims}/{name}.out"\ncat "{shims}/{name}.err" >&2\nexit {returncode}\n',
                encoding="utf-8",
            )
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        sbatch_next = shims / "sbatch.next"
        if not sbatch_next.exists():
            sbatch_next.write_text(str(self.next_job_id), encoding="utf-8")
        (shims / "sbatch.err").write_text(self.sbatch_stderr, encoding="utf-8")
        sbatch = shims / "sbatch"
        sbatch.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "--version" ]; then echo "slurm 23.11.0"; exit 0; fi\n'
            'if [ "$1" = "--help" ]; then echo "--dependency --kill-on-invalid-dep"; exit 0; fi\n'
            f'echo "$*" >> "{shims}/sbatch.args"\n'
            f'count=$(cat "{shims}/sbatch.count" 2>/dev/null || echo 0)\n'
            f'echo $((count + 1)) > "{shims}/sbatch.count"\n'
            f"if [ {self.sbatch_returncode} -ne 0 ]; then\n"
            f'  cat "{shims}/sbatch.err" >&2\n'
            f"  exit {self.sbatch_returncode}\n"
            "fi\n"
            f'job=$(cat "{sbatch_next}")\n'
            f'echo $((job + 1)) > "{sbatch_next}"\n'
            'echo "$job"\n',
            encoding="utf-8",
        )
        sbatch.chmod(sbatch.stat().st_mode | stat.S_IEXEC)
        scancel = shims / "scancel"
        scancel.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        scancel.chmod(scancel.stat().st_mode | stat.S_IEXEC)
        return shims

    def exec_python(
        self,
        script: str,
        args: Sequence[str] = (),
        *,
        timeout: float = 60.0,
        check: bool = True,
    ) -> ExecResult:
        self.calls.append("python3 - " + " ".join(str(a) for a in args))
        scripted = self._prepare_call(self._python_call_keys(args))
        if scripted is not None:
            if not isinstance(scripted, ExecResult):
                raise TypeError("scripted helper outcome must be ExecResult or an exception")
            if check and scripted.returncode != 0:
                raise TransportError(
                    "fake python failed",
                    returncode=scripted.returncode,
                    stderr=scripted.stderr,
                )
            return scripted
        shims = self._slurm_shims()
        proc = subprocess.run(
            [sys.executable, "-", *[str(a) for a in args]],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        result = ExecResult(proc.returncode, proc.stdout, proc.stderr)
        next_path = shims / "sbatch.next"
        if next_path.is_file():
            self.next_job_id = int(next_path.read_text(encoding="utf-8"))
        if args and args[0] == "submit-run":
            payloads = parse_json_lines(result.stdout)
            if payloads and payloads[0].get("status") == "submitted" and payloads[0].get("source") == "sbatch":
                script = self._option_value(args, "--script")
                if script:
                    self._simulate_submission(Path(script), str(payloads[0]["job_id"]))
        if check and result.returncode != 0:
            raise TransportError("fake python failed", returncode=result.returncode, stderr=result.stderr)
        return result

    def exec_json(self, script: str, args: Sequence[str] = (), *, timeout: float = 60.0) -> object:
        self._prepare_call(("exec_json",))
        result = self.exec_python(script, args, timeout=timeout)
        payloads = parse_json_lines(result.stdout)
        if not payloads:
            raise TransportError("no structured result", stderr=result.stderr)
        return payloads[0]

    def upload(
        self,
        source: str,
        dest: str,
        *,
        delete: bool = False,
        filters: Sequence[str] = (),
        timeout: float = 600.0,
        files_from: Sequence[str] | None = None,
    ) -> SyncStats:
        self.uploads.append((source, dest))
        scripted = self._prepare_call(("upload",))
        if scripted is not None:
            if not isinstance(scripted, SyncStats):
                raise TypeError("scripted upload outcome must be SyncStats or an exception")
            return scripted
        dest_path = Path(dest)
        if files_from is not None:
            for rel in files_from:
                target = dest_path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(source) / rel, target)
        elif source.endswith("/"):
            if delete and dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(source, dest_path, dirs_exist_ok=True)
        else:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest_path)
        return SyncStats()

    def download(
        self,
        source: str,
        dest: str,
        *,
        filters: Sequence[str] = (),
        timeout: float = 600.0,
    ) -> SyncStats:
        self.downloads.append((source, dest, tuple(filters)))
        scripted = self._prepare_call(("download",))
        if scripted is not None:
            if not isinstance(scripted, SyncStats):
                raise TypeError("scripted download outcome must be SyncStats or an exception")
            return scripted
        source_path = Path(source.rstrip("/"))
        if source_path.is_dir():
            shutil.copytree(source_path, Path(dest), dirs_exist_ok=True)
        return SyncStats()

    def stream(self, command: str, *, on_line: Callable[[str], None]) -> StreamHandle:
        self.calls.append(command)
        scripted = self._prepare_call((f"stream:{command}", "stream"))
        if scripted is not None:
            if not isinstance(scripted, StreamHandle):
                raise TypeError("scripted stream outcome must be StreamHandle or an exception")
            return scripted
        tokens = shlex.split(command)
        path = Path(tokens[-1])
        stop = threading.Event()

        def pump() -> None:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if stop.is_set():
                        return
                    on_line(line)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return StreamHandle(_close=stop.set, _wait=lambda: (thread.join(), 0)[1])

    def connect(self) -> None:
        self._prepare_call(("connect",))
        self.connected = True

    def disconnect(self) -> None:
        self._prepare_call(("disconnect",))
        self.connected = False

    def alive(self) -> bool:
        self._prepare_call(("alive",))
        return self.connected

    # -- default command emulation ------------------------------------------------

    def _default_exec(self, command: str) -> ExecResult:
        if command == "true":
            return ExecResult(0, "", "")
        if command.startswith("test -f "):
            path = shlex.split(command)[-1]
            return ExecResult(0 if Path(path).is_file() else 1, "", "")
        if command.startswith("test -d "):
            path = shlex.split(command)[-1]
            return ExecResult(0 if Path(path).is_dir() else 1, "", "")
        if command.startswith("test -w "):
            path = shlex.split(command)[-1]
            return ExecResult(0 if Path(path).exists() else 1, "", "")
        if command.startswith("mkdir -p "):
            for path in shlex.split(command)[2:]:
                Path(path).mkdir(parents=True, exist_ok=True)
            return ExecResult(0, "", "")
        if command.startswith("rm -rf ") and "&&" in command:
            rm_part, mkdir_part = command.split("&&", 1)
            target = shlex.split(rm_part.strip())[-1]
            if Path(target).exists():
                shutil.rmtree(target)
            made = shlex.split(mkdir_part.strip())[-1]
            Path(made).mkdir(parents=True, exist_ok=True)
            return ExecResult(0, "", "")
        if command.startswith("rm -rf "):
            target = shlex.split(command)[-1]
            if Path(target).exists():
                shutil.rmtree(target)
            return ExecResult(0, "", "")
        if command.startswith("sbatch --parsable "):
            return self._sbatch(command)
        if command.startswith("squeue "):
            return ExecResult(0, self.squeue_output, "")
        if command.startswith("sacct "):
            return ExecResult(0, self.sacct_output, "")
        if command.startswith("scancel "):
            return ExecResult(0, "", "")
        if command.startswith("tail "):
            tokens = shlex.split(command)
            path = Path(tokens[-1])
            if not path.exists():
                return ExecResult(1, "", "no such file")
            lines = path.read_text(encoding="utf-8").splitlines()
            count = int(tokens[2]) if tokens[1] == "-n" else 10
            return ExecResult(0, "\n".join(lines[-count:]) + "\n", "")
        if command.startswith("command -v "):
            return ExecResult(0, "/usr/bin/fake\n", "")
        if command.startswith("cat > "):
            header, _, body = command.partition("\n")
            path = shlex.split(header.removeprefix("cat > ").split("<<")[0].strip())[0]
            content = body.rsplit("\nSLURMDECK_EOF", 1)[0]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content + "\n", encoding="utf-8")
            return ExecResult(0, "", "")
        if command.startswith("bash --noprofile --norc -s"):
            return ExecResult(0, "", "")
        raise AssertionError(f"FakeTransport has no handler for command: {command!r}")

    def _sbatch(self, command: str) -> ExecResult:
        script_path = Path(shlex.split(command)[-1])
        if self.sbatch_returncode != 0:
            return ExecResult(self.sbatch_returncode, "", self.sbatch_stderr)
        job_id = str(self.next_job_id)
        self.next_job_id += 1
        shim_next = self.root / ".shims" / "sbatch.next"
        if shim_next.parent.is_dir():
            shim_next.write_text(str(self.next_job_id), encoding="utf-8")
        self._simulate_submission(script_path, job_id)
        return ExecResult(0, job_id + "\n", "")

    def _simulate_submission(self, script_path: Path, job_id: str) -> None:
        if not self.simulate_execution or not script_path.exists():
            return
        run_root = script_path.parent
        tasks_file = run_root / "tasks.jsonl"
        task_count = len(tasks_file.read_text(encoding="utf-8").splitlines()) if tasks_file.exists() else 0
        code_dir = self._code_dir_from_sbatch(script_path.read_text(encoding="utf-8"))
        for index in range(task_count):
            env = {
                "SLURM_ARRAY_JOB_ID": job_id,
                "SLURM_ARRAY_TASK_ID": str(index),
                "PATH": "/usr/bin:/bin",
                **self.env_overrides,
            }
            if index in self.fail_task_indices:
                env["SLURMDECK_FAKE_FAIL"] = "1"
            subprocess.run(
                [
                    sys.executable,
                    str(run_root / "agent.py"),
                    "exec",
                    "--run-root",
                    str(run_root),
                    "--code-dir",
                    code_dir or str(run_root),
                    "--index",
                    str(index),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

    @staticmethod
    def _option_value(args: Sequence[str], option: str) -> str | None:
        try:
            index = list(args).index(option)
        except ValueError:
            return None
        return str(args[index + 1]) if index + 1 < len(args) else None

    @staticmethod
    def _code_dir_from_sbatch(text: str) -> str | None:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("--code-dir"):
                return shlex.split(line.replace("\\", ""))[1]
        return None
