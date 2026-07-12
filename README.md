# SlurmDeck

SlurmDeck is a local command-line tool and terminal UI for running code on a
remote [Slurm](https://slurm.schedmd.com/) cluster. You keep editing your
project on your workstation; SlurmDeck snapshots the exact files needed for a
run, transfers them over SSH, submits a command or parameter sweep, combines
Slurm and per-task status, follows logs, and downloads results.

It is intended for researchers and engineers who already have SSH access to a
Slurm cluster and want a reproducible alternative to repeatedly assembling
`rsync`, `sbatch`, `squeue`, `sacct`, and result-copy commands by hand.
SlurmDeck works with the cluster's existing accounts, partitions, and policies.
It does not replace Slurm, and it does not require a service or SlurmDeck
package to be installed on the cluster.

A typical workflow is:

1. Register a cluster once on your workstation.
2. Initialize SlurmDeck inside a local project.
3. Review the exact files that will be included.
4. Plan and submit a command or parameter sweep.
5. Check task status, follow logs, and pull results back locally.

## Installation

`pipx` is the simplest option if it is already installed:

```bash
pipx install slurmdeck
```

Otherwise, create a dedicated virtual environment:

```bash
mkdir -p "$HOME/.venvs"
python3 -m venv "$HOME/.venvs/slurmdeck"
source "$HOME/.venvs/slurmdeck/bin/activate"
python -m pip install --upgrade pip
python -m pip install slurmdeck
slurmdeck --version
```

When using the virtual-environment method, run the `source` command again in
each new shell before using `slurmdeck`.

| Location | Requirements |
| --- | --- |
| Local workstation | POSIX system, Python 3.11+, `git`, OpenSSH, and `rsync` |
| Remote login node | Python 3.8+ and `sbatch`, `squeue`, `sacct`, and `scancel` |
| Remote filesystem | A base directory visible from the login and compute nodes |

## Quick start

The example below uses:

- `hpc` as a local nickname for the cluster;
- `user@login.example.com` as the SSH destination;
- `$HOME/slurmdeck` as the remote base directory; and
- `/path/to/my-project` as the local project.

Replace those values with your own. If the compute nodes cannot access your
remote home directory, use a shared project, data, or scratch path for
`--base` instead.

### 1. Verify SSH and Slurm

Connect with OpenSSH before configuring SlurmDeck. Review the host fingerprint
when SSH asks, then confirm that Python and the Slurm client commands are
available:

```bash
ssh user@login.example.com

# These commands run on the remote login node.
python3 --version
command -v sbatch
command -v squeue
command -v sacct
command -v scancel
exit
```

### 2. Register and connect to the cluster

Add the cluster once on this workstation. The single quotes around
`$HOME/slurmdeck` prevent your local shell from expanding `$HOME`; the path
is resolved on the remote instead.

```bash
slurmdeck remote add hpc \
  --host user@login.example.com \
  --base '$HOME/slurmdeck' \
  --use
slurmdeck remote connect hpc
slurmdeck remote status hpc
```

If you already have a `Host` entry in `~/.ssh/config`, use
`--ssh-alias your-alias` instead of `--host user@login.example.com`.

### 3. Initialize a local project

Run `init` from the project directory that contains the code you want to
submit:

```bash
cd /path/to/my-project
slurmdeck init --remote hpc
slurmdeck doctor
```

`init` creates local SlurmDeck state under `.slurmdeck/`. `doctor` checks
the local tools, SSH connection, Slurm commands, and project configuration. A
missing ClusterProfile warning is expected unless you plan to let SlurmDeck
build managed environments.

### 4. Review the snapshot

Before the first submission, inspect the exact project files SlurmDeck would
upload:

```bash
slurmdeck snapshot preview
```

Check that required source/configuration files are present and that datasets,
credentials, private keys, and generated outputs are absent. Adjust the sync
rules in `.slurmdeck/project.yaml` if needed, then preview again.

### 5. Plan, review, and submit the first run

Everything after `--` belongs to your program. Replace `python train.py`
with a command that exists in your project:

```bash
slurmdeck submit --plan-only --time 00:10:00 -- python train.py

# Copy the Run value printed above.
RUN_ID="paste-the-run-id-here"
slurmdeck run show "$RUN_ID"
slurmdeck run submit "$RUN_ID"
```

`--plan-only` creates the local immutable run and snapshot but does not call
`sbatch`. Once the plan looks right, `run submit` submits that exact run.
For later runs, the two steps may be combined:

```bash
slurmdeck submit --time 04:00:00 --gres gpu:1 -- python train.py
```

### 6. Follow the run and download results

Use the same Run ID printed during planning or submission:

```bash
slurmdeck run status "$RUN_ID"
slurmdeck run status "$RUN_ID" --watch
slurmdeck run logs "$RUN_ID" --follow
slurmdeck run pull "$RUN_ID" --into "./results/$RUN_ID"
```

Press `Ctrl+C` to stop `--watch` or `--follow`; this does not cancel the
Slurm job. Use `slurmdeck run cancel "$RUN_ID"` when cancellation is intended.

### 7. Open the terminal UI

The TUI provides the same project runs, environments, remotes, logs, and
operations in a full-screen interface:

```bash
slurmdeck ui
```

Use `1`, `2`, and `3` to switch between Runs, Environments, and Remotes;
press `?` for the keys available on the current screen.

The basic flow above uses the batch job's default remote environment. Managed
or existing environments are configured explicitly in `.slurmdeck/project.yaml`
as described in [Environments](#environments).

## Core concepts

- **Remote** — an SSH destination and a remote base directory. An optional
  **ClusterProfile** records user-supplied site policy: permitted environment
  executors, login-node policy, shared-filesystem guarantees, modules, conda,
  network/channel access, Slurm defaults/dependency behavior, and platform.
- **Project** — a local directory initialized with `slurmdeck init`. The
  generated config has a UUID `project_id`, a `display_name`, resource/env
  intent, and sync rules. Run/task state is stored locally in SQLite.
- **Run** — one immutable materialized run. Planning is validated in memory,
  then committed atomically to the local run directory and database. A retry
  creates a new run and preserves the original resources and task templates.
- **Snapshot** — the content hash of the exact selected code files. Snapshots
  upload once, are reference-derived from manifests/receipts, and are garbage
  collected only by an explicit, lock-protected command.
- **Environment** — either a managed conda environment with immutable
  generations or an external prefix owned by the user. Remote registry records
  attempts, receipts, provenance, scheduler state, errors, and the active
  generation.
- **Agent** — one of two stdlib-only Python helpers: the run agent executes and
  scans tasks; the environment agent owns registry/executor operations.

## Environments

Project intent belongs in `.slurmdeck/project.yaml`:

```yaml
env:
  type: conda
  name: ml
  spec_file: environment.yml
  modules: [cuda/12.1]
  post_install: ["pip install -e ."]
  smoke_test: "python -c 'import torch'"
  channel_priority: strict
  solver: libmamba
  build_resources:
    time: "01:00:00"
    mem: 8G
```

Managed environment builds also require an explicit ClusterProfile describing
site policy and capabilities. Adapt the complete
[ClusterProfile example](https://github.com/dejay-vu/slurmdeck/blob/main/docs/configuration.md#remote-and-clusterprofile),
then validate it before preparing the environment:

```bash
slurmdeck remote profile set hpc --file cluster-profile.yaml
slurmdeck remote profile show hpc
slurmdeck doctor
slurmdeck env plan
slurmdeck env prepare
```

`environment.yml` is the sole channel declaration source. The environment ID
is `<safe-name>-<hash12>`; the full SHA-256 covers spec bytes, modules,
post-install/smoke commands, and solver/channel policy, but not scheduler
resources.

```bash
slurmdeck env plan                         # no writes
slurmdeck env prepare --no-wait            # start once, return QUEUED/BUILDING
slurmdeck env list
slurmdeck env status <env_id>
slurmdeck env logs <env_id> --follow
slurmdeck env cancel <env_id> --yes
slurmdeck env prepare --rebuild            # new immutable generation
slurmdeck env remove <env_id> --yes
slurmdeck env gc                           # dry run; add --yes to delete
```

The default run policy is `--env-wait ready`. `--env-wait afterok` may submit a
run while a Slurm environment build is queued, but only when the ClusterProfile
explicitly guarantees both `afterok` support and invalid-dependency
termination. The run is shown as `WAITING_FOR_ENV`; a failed or cancelled
build becomes `ENV_BUILD_FAILED` or `ENV_BUILD_CANCELLED` without executing
tasks. Login builds never qualify for `afterok`.

Login-node builds are available only when a user explicitly declares them
allowed. They are not a queue-avoidance shortcut: login nodes can have a
different OS, CPU, filesystem, network policy, and acceptable-use policy from
compute nodes. On clusters that forbid sustained work on login nodes, use the
Slurm executor for environment builds.

## Sweeps and placeholders

```yaml
# sweep.yaml
version: 1
parameters:
  lr: [1.0e-3, 1.0e-4]
  model: [small, large]
exclude:
  - {lr: 1.0e-4, model: large}
config:
  training:
    lr: "{lr}"
    model: "{model}"
env:
  RUN_TAG: "{model}-lr{lr}"
arg_style: posix
```

```bash
slurmdeck sweep validate sweep.yaml
slurmdeck sweep preview sweep.yaml
slurmdeck submit --sweep sweep.yaml -- python train.py {args}
```

Commands, sweep args, environment values, and generated configs share these
locally resolved placeholders: `{config}`, `{output}`, `{task_id}`,
`{task_name}`, `{run}`, `{index}`, `{args}`, and sweep parameters. Literal
braces are `{{` and `}}`. Unknown placeholders fail before materialization.

## CLI contracts

```text
slurmdeck init | doctor | ui | --version
slurmdeck remote add|list|use|remove|connect|disconnect|status|exec
slurmdeck remote profile show|set
slurmdeck env plan|prepare|list|show|status|logs|cancel|remove|gc
slurmdeck snapshot preview|list|gc
slurmdeck sweep validate|preview
slurmdeck submit [--plan-only] [--env-wait ready|afterok] [resources...] -- CMD...
slurmdeck run list|show|submit|reconcile|status|logs|cancel|retry|pull|clean
```

Machine-readable commands emit exactly one JSON document:

```json
{"schema_version": 1, "ok": true, "data": {}, "meta": {}, "error": null}
```

Application errors use the same envelope on stdout with empty stderr. Typer
usage errors remain native. `--json` is rejected with `--watch`/`--follow`.
Exit codes are `0` success, `1` user/configuration error, `2` usage, `3`
transport, and `4` partial or empty completion. Partial pull/clean reports
preserve retryable paths and per-location outcomes.

CLI layouts adapt at 120 and 80 columns without truncating identifiers or
timestamps. `SLURMDECK_THEME=dark|light|mono` selects the shared semantic
theme; `slurmdeck ui --theme ...` overrides it for one TUI session, and
`NO_COLOR` forces the monochrome/plain contract.

## Safety and recovery

- Submission uses a full token, a remote lock, an atomic receipt, a token in
  the Slurm comment, and a short job-name suffix. An unknown `sbatch` outcome
  is never automatically resubmitted; use `slurmdeck run reconcile`.
- Status merges live scheduler observations, task artifacts, and local
  lifecycle state. A failed refresh returns the last good snapshot as stale
  when one exists.
- Environment prepare locks the full hash. Lost responses attach to the same
  attempt; a possibly submitted build stays `BUILD_UNKNOWN`; a provably
  interrupted pre-sbatch staging attempt becomes safely retryable.
- Managed environment promotion changes the registry's active pointer only
  after build, activation, smoke, and package-channel verification succeed.
- `run clean` reports local, remote, receipt, and snapshot-reference outcomes.
  A partial remote clean retains the local run as a recovery handle.
- SlurmDeck only modifies paths within its documented local and remote layouts;
  unrecognized paths are left untouched.
- Snapshot selection refuses common secret-bearing files and private-key
  content by default. Run `slurmdeck snapshot preview` to inspect the exact
  local files before submission.

## TUI

![SlurmDeck TUI showing the wide Runs screen in the dark theme](https://raw.githubusercontent.com/dejay-vu/slurmdeck/main/docs/assets/slurmdeck-tui.svg)

The wide layout keeps the run list and detail pane side by side. At
compact terminal widths, the same information moves to a dedicated detail
screen.

The TUI supports New Run (simple command or existing Sweep YAML), Add Remote
(host or SSH alias), explicit ClusterProfile import/edit/diff/save,
environment prepare/attach/log/cancel/rebuild/remove/GC, responsive
master-detail screens, persistent dismissible errors, stale status, elapsed
operations, and shared dark/light/mono styling. At 80 columns, Enter opens a
dedicated detail page; at 100 columns and above, lists and details share the
screen. See the [TUI documentation](https://github.com/dejay-vu/slurmdeck/blob/main/docs/tui.md).

## Documentation

- [Configuration](https://github.com/dejay-vu/slurmdeck/blob/main/docs/configuration.md)
- [Architecture](https://github.com/dejay-vu/slurmdeck/blob/main/docs/architecture.md)
- [Remote protocol](https://github.com/dejay-vu/slurmdeck/blob/main/docs/remote-protocol.md)
- [Sweeps](https://github.com/dejay-vu/slurmdeck/blob/main/docs/sweeps.md)
- [TUI](https://github.com/dejay-vu/slurmdeck/blob/main/docs/tui.md)
- [Changelog](https://github.com/dejay-vu/slurmdeck/blob/main/CHANGELOG.md)
- [Contributing](https://github.com/dejay-vu/slurmdeck/blob/main/CONTRIBUTING.md)
- [Security policy](https://github.com/dejay-vu/slurmdeck/blob/main/SECURITY.md)

## Development

```bash
git clone https://github.com/dejay-vu/slurmdeck.git
cd slurmdeck
python -m pip install -e ".[dev]"
```

See [CONTRIBUTING.md](https://github.com/dejay-vu/slurmdeck/blob/main/CONTRIBUTING.md)
for the complete development workflow.

## License

MIT.
