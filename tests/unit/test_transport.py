"""SshTransport child-process hygiene."""

from __future__ import annotations

import subprocess

import pytest

from slurmdeck.models.remote import HostKeyPolicy, Remote
from slurmdeck.transport import TransportError
from slurmdeck.transport.ssh import SshTransport, clean_child_env, parse_rsync_stats


def test_rsync_stats_capture_counts_bytes_and_partial_paths():
    stats = parse_rsync_stats(
        """Number of files: 8 (reg: 5, dir: 3)
Number of regular files transferred: 2
Total transferred file size: 2,048 bytes
""",
        'rsync: [sender] send_files failed to open "/base/run/results/003/out": Permission denied (13)\n',
        returncode=23,
    )

    assert stats.matched_files == 5
    assert stats.transferred_files == 2
    assert stats.skipped_files == 2
    assert stats.failed_files == 1
    assert stats.bytes_transferred == 2048
    assert stats.failed_paths == ("/base/run/results/003/out",)
    assert stats.returncode == 23


class TestCleanChildEnv:
    def test_loader_overrides_dropped(self, monkeypatch):
        # a conda env's libcrypto on LD_LIBRARY_PATH makes system ssh abort
        # with "OpenSSL version mismatch"
        blocked = {
            "DYLD_FALLBACK_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "OPENSSL_CONF",
            "OPENSSL_MODULES",
        }
        for key in blocked:
            monkeypatch.setenv(key, f"/bad/{key.lower()}")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = clean_child_env()
        assert blocked.isdisjoint(env)
        assert env["PATH"] == "/usr/bin"


class TestSshTransportSpawns:
    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            (HostKeyPolicy.INHERIT, None),
            (HostKeyPolicy.STRICT, "StrictHostKeyChecking=yes"),
            (HostKeyPolicy.ACCEPT_NEW, "StrictHostKeyChecking=accept-new"),
        ],
    )
    def test_host_key_policy_only_overrides_openssh_config_when_explicit(self, policy, expected, tmp_path):
        remote = Remote(name="x", host="user@host.example", base="/base", host_key_policy=policy)
        options = SshTransport(remote, control_dir=tmp_path)._ssh_options()
        strict_options = [value for value in options if value.startswith("StrictHostKeyChecking=")]

        assert strict_options == ([] if expected is None else [expected])

    def test_exec_uses_sanitized_env_and_no_tty_stdin(self, monkeypatch, tmp_path):
        recorded: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setenv("LD_LIBRARY_PATH", "/poison/lib")
        transport = SshTransport(Remote(name="x", host="user@host.example", base="/base"), control_dir=tmp_path / "cm")
        transport.exec("true")

        env = recorded["env"]
        assert isinstance(env, dict)
        assert "LD_LIBRARY_PATH" not in env
        # ssh must never read the terminal (it would steal TUI keystrokes)
        assert recorded["stdin"] is subprocess.DEVNULL

    def test_directory_upload_creates_the_remote_destination_within_the_rsync_call(self, monkeypatch, tmp_path):
        recorded: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        transport = SshTransport(Remote(name="x", host="user@host.example", base="/base"), control_dir=tmp_path)

        transport.upload("/local/inbox/", "/remote/envs/inbox/attempt-1/", delete=True)

        argv = recorded["argv"]
        assert isinstance(argv, list)
        assert "--rsync-path=mkdir -p /remote/envs/inbox/attempt-1 && rsync" in argv
        assert "--stats" in argv

    def test_download_returns_partial_stats_for_rsync_code_23(self, monkeypatch, tmp_path):
        def fake_run(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv,
                23,
                stdout=(
                    "Number of files: 3 (reg: 2, dir: 1)\n"
                    "Number of regular files transferred: 1\n"
                    "Total transferred file size: 12 bytes\n"
                ),
                stderr='rsync: open "/base/run/bad": Permission denied\n',
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        transport = SshTransport(Remote(name="x", host="user@host.example", base="/base"), control_dir=tmp_path)

        stats = transport.download("/base/run/", str(tmp_path / "out"))

        assert stats.returncode == 23
        assert stats.transferred_files == 1
        assert stats.failed_files == 1

    def test_exec_normalizes_ssh_launch_oserror_with_command_and_cause(self, monkeypatch, tmp_path):
        cause = FileNotFoundError("ssh executable missing")

        def missing_ssh(*_args, **_kwargs):
            raise cause

        monkeypatch.setattr(subprocess, "run", missing_ssh)
        transport = SshTransport(Remote(name="x", host="user@host.example", base="/base"), control_dir=tmp_path)

        with pytest.raises(TransportError, match="Could not launch ssh") as caught:
            transport.exec("python3 -", input_text="print('hello')")

        assert caught.value.command == "python3 -"
        assert caught.value.__cause__ is cause
        assert caught.value.error.context == {"command": "python3 -", "returncode": None}
        assert caught.value.error.underlying_cause is cause
