# Architecture

SlurmDeck `0.1.0` is a clean schema-v1 implementation. Interfaces depend
on services and service-owned views; presentation never re-derives domain
state.

```text
interfaces     cli/ (Typer + Rich)             tui/ (Textual + Rich)
presentation   ThemeSpec · semantic roles · adaptive OutputManager
services       runs/status/snapshots/results/logs/remotes/doctor
               cluster/env_planning/env_execution/env_lifecycle/env_binding
planning       run planner · placeholders · command line · sweep expansion
remote agents  agent.py (runs)                 env_agent.py (environments)
transport      Transport protocol · SshTransport · SSH/rsync ControlMaster
storage        paths · YAML/user store · SQLite/repositories
models         strict Pydantic v2 contracts, schema version 1
```

## Domain boundaries

### Runs

`RunPlanner.plan()` is pure with respect to persisted run state: it validates
the command/sweep, resolves every task, selects the snapshot input set, and
renders the complete run in memory. `RunMaterializer.commit()` owns the
mutation boundary:

1. write an attempt-specific staging tree;
2. begin one SQLite transaction and insert run/tasks (including
   `PENDING=<task count>`);
3. atomically rename staging to the final run directory;
4. commit SQLite;
5. write the commit marker.

Every boundary has compensation/recovery coverage. `RunRecoveryService`
repairs residue only before mutating run workflows; reads and Doctor never
trigger repair.

Runs are immutable. Submit, retry, and rebuild do not mutate an earlier run's
command, resources, snapshot, or environment generation. Retry materializes a
new run from stored task templates and original effective resources.

### Submission

`RunSubmissionClient` invokes the stdlib-only run helper with a unique full
token. The remote helper locks that token, writes an atomic receipt, includes
the token in the Slurm comment and a suffix in the job name, and executes
`sbatch` at most once.

The local lifecycle distinguishes `submit_failed` (known rejection, safe to
retry with a new token) from `submit_unknown` (job may exist, automatic retry
forbidden). `run reconcile` searches in fixed order: receipt, Slurm comment,
then job-name suffix. It never calls `sbatch`.

### Status truth

Remote scans preserve `SchedulerObservation` fields rather than projecting
them early: job/task ID, state, reason, exit code, source, and observation
time. Repositories store scheduler and artifact observations separately.

`StatusService.snapshot()` is the only public run-status read path used by CLI
and TUI. It applies the domain precedence centrally:

```text
live/terminal scheduler evidence
        ↓
terminal task artifact evidence
        ↓
local run/task lifecycle fallback
```

This preserves scheduler reasons such as `Priority`, distinguishes stale data,
and projects environment dependencies to `WAITING_FOR_ENV`,
`ENV_BUILD_FAILED`, or `ENV_BUILD_CANCELLED`. A refresh failure returns the
last good snapshot with stale metadata when available; without any snapshot it
is an error.

One remote run scan batches task artifacts, `squeue`, `sacct`, and exact
environment-binding checks. SQLite updates only changed observations. Local
rendering and remote refresh are timed independently.

### Snapshots

Snapshot selection and hashing use the same top-down, directory-pruned file
set. A snapshot's code directory is not valid until its metadata marker is
written atomically after upload. References are derived dynamically from
snapshot markers, run manifests, and active submission receipts rather than a
mutable counter.

GC holds a remote namespace lock and considers only valid, unreferenced
snapshots at least 24 hours old. It is dry-run by default. `run clean` releases
run/receipt references but never invokes global snapshot GC.

### Cluster policy and observation

The architecture keeps three values separate:

- `ClusterProfile`: explicit, user-owned permission and site policy;
- `ClusterObservation`: read-only evidence from one remote probe;
- `EffectiveClusterContract`: the resolved executor/resources/dependency gate.

Observation can contradict policy but can never grant permission. Doctor only
diagnoses. Profile writes go through the validated, atomic `remote profile set`
service (or the confirmed TUI profile form).

Mutating connect/prepare workflows may persist an advisory observation for 24
hours. Cached records are identity-bound and never replace remote registry
truth or submission-time validation.

### Environments

`EnvironmentPlanner` is a pure identity/capability planner. It combines project
intent, ClusterProfile, observation, registry records, and optional executor
selection into an `EnvironmentPlan` with all missing fields, conflicts,
warnings, effective resources, and one of `reuse`, `attach`, `create`,
`rebuild`, or `verify`.

The environment registry is remote-global and keyed by
`<safe-name>-<hash12>` while retaining the full SHA-256. A managed record owns
attempts and immutable generations; an external record only points to a
user-owned prefix. Run references are dynamic `EnvironmentView` data, never a
stored counter.

Prepare uses one full-hash lock:

1. validate and plan locally;
2. upload one attempt inbox for create/rebuild;
3. under the lock, re-read authoritative registry/receipt state;
4. reuse/attach or create one attempt and invoke one selected executor;
5. build directly into the unpublished final generation prefix;
6. activation/smoke/channel verification;
7. atomically switch the active registry pointer.

The Slurm executor records a build job; the login executor records host, PID,
heartbeat, logs, and completion receipt. Login is policy-gated and cannot
provide `afterok`. Missing or ambiguous submission evidence becomes
`BUILD_UNKNOWN`, never an automatic duplicate. A receipt proving interruption
before `sbatch` makes only that staging attempt safely retryable.

Managed removal first validates references and atomically moves generations to
trash. Background deletion can be reconciled to `REMOVED` or
`REMOVE_UNKNOWN`; GC reports trash and failed unpublished generations. External
remove unregisters but never deletes the prefix.

### Presentation and operations

`ThemeSpec` is the only source of semantic colors/styles and creates both Rich
and Textual themes. Domain statuses map to semantic roles; interfaces retain
text labels so color is never the sole signal. Dark, light, mono, and
`NO_COLOR` share the same roles. An interactive theme choice is atomically
saved in user `state.yaml` and becomes the shared CLI/TUI default; process-level
options and environment variables override it without rewriting it.

`OutputManager` selects a full table at 120+ columns, a two-line record at
80–119, and key/value blocks below 80. Non-TTY records are complete and plain.
Wide tables use semantic title/header/key colors and a single horizontal rule
below the header; they have no outer or vertical borders. JSON is one stable
envelope with no progress noise.

Services emit typed `OperationEvent`s. CLI TTY feedback appears only after
200 ms to avoid spinner flash; non-TTY writes phase boundaries; JSON uses a
silent sink. The TUI uses separate read workers and an exclusive mutation
group, showing elapsed operation time, last success/staleness, and persistent
dismissible errors. Doctor reports its local-tool, connection, remote-probe,
and project-validation phases through the same event path.

## Data flow: environment-backed submit

```text
ProjectConfig + ClusterProfile + observation + registry
                    │
                    ▼
            EnvironmentPlan / exact EnvBinding
                    │
          ready ────┴──── afterok (capability-gated)
                    │
                    ▼
RunPlanner → RunMaterializer → snapshot/run upload → token receipt → sbatch
                    │                                  │
                    └──────── StatusService scan ──────┘
                                  │
                                  ▼
                        CLI/TUI service-owned views
```

`ready` requires a verified generation before run upload. `afterok` binds the
future immutable generation and build job, then adds
`--dependency=afterok:<job>`; per-job policy also adds
`--kill-on-invalid-dep=yes`. A shared environment build is not cancelled when
one dependent run is cancelled.

## Failure and cleanup contracts

- Invalid planning has no persisted side effects.
- Run materialization is compensated at staging/insert/rename/commit/marker
  boundaries.
- Known `sbatch` rejection is retryable; unknown outcome is reconcile-only.
- Environment staging, receipts, scheduler submission, heartbeat, build,
  smoke, promotion, cancellation, trash move, and deletion have explicit
  durable states.
- Partial pull reports matched/transferred/skipped/failed/bytes and relative
  retry paths. An empty pull is not a successful completion.
- Partial clean reports local, remote, receipt, and snapshot-reference
  outcomes; local state remains as the retry handle until remote cleanup is
  confirmed.

## State/version policy

The database, ProjectConfig, run/status manifests, environment registry,
receipts, and JSON envelope all begin at schema version 1 for `0.1.0`.
Only schema version 1 is supported. Unrecognized remote environment trees are
ignored and excluded from GC scope.
