# Remote protocol

SlurmDeck installs no service or package on a cluster. The local client uses
system `ssh`/`rsync` through the `Transport` interface and streams one of two
stdlib-only Python helpers to remote Python 3.8 or newer:

- `agent.py` executes/scans runs and owns run submission, cleanup, and snapshot
  lifecycle operations;
- `env_agent.py` owns the environment registry, executors, reconciliation,
  removal, and GC.

Structured stdout records are prefixed with `SLURMDECK_JSON\t`; login banners
and other unprefixed output cannot corrupt decoding. Helper inputs are explicit
arguments or strict JSON payloads. Remote helpers do not import SlurmDeck.

## Schema-v1 layout

Every path is derived from the connected remote's resolved `<base>`:

```text
runs/<run_id>/
snapshots/<sha256>/code/
snapshots/<sha256>/.complete.json

envs/registry/<env_id>.json
envs/generations/<env_id>/<generation_id>/
envs/attempts/<env_id>/<attempt_id>/
envs/inbox/<attempt_id>/
envs/trash/<env_id>/

locks/run/<submission_token>.lock
locks/env/<full_hash>/.lock
locks/snapshot-gc.lock
receipts/run/<submission_token>.json
receipts/env/<attempt_id>.json
```

Paths that do not match this layout are not interpreted and are outside GC
scope.

## Run directory and manifest

Each committed run uploads a self-contained `<base>/runs/<run_id>/`:

| Path | Purpose |
| --- | --- |
| `run.json` | schema-v1 immutable run manifest, project identity, snapshot, exact `EnvBinding` |
| `tasks.jsonl` | one fully resolved task per line |
| `agent.py` | packaged stdlib-only run helper |
| `activation.sh` | exact bound environment activation, possibly empty |
| `submit.sbatch` | Slurm directives and `agent.py exec` invocation |
| `configs/` | generated per-task YAML |
| `logs/` | scheduler stdout/stderr (`task_%A_%a.{out,err}`) |
| `results/<task_id>/` | task output plus atomic `status.json` |

A task record contains either `argv` or a fully resolved `shell` string, plus
task ID/name, environment, config path, and result path. All relative paths are
validated and rooted under the run. No template or shell `eval` is deferred to
the cluster.

## Task execution

`agent.py exec`:

1. atomically writes `RUNNING` to `results/<task>/status.json`;
2. sources `activation.sh` under `bash --noprofile --norc` and captures the
   resulting environment as JSON;
3. layers SlurmDeck variables and task-specific environment on top;
4. executes argv directly (or the explicit shell command), forwarding signals;
5. atomically writes `COMPLETED`, `FAILED`, or `KILLED` with exit/reason,
   timestamps, host, and Slurm job/task ID.

Activation failure records a terminal artifact and never runs the user
command. Task environment wins over activation values.

## Batched status scan

The client pipes `agent.py scan --base ... --run ... --jobs ... --since ...`
once per refresh. The helper emits:

- a scan header and watermark;
- changed task artifacts (`status.json` mtime greater than `--since`);
- `squeue` observations with state and reason;
- `sacct` observations with state and exit code;
- exact environment-binding/dependency observations.

Scheduler source/time/reason/exit fields remain separate from artifacts in
SQLite. The client merges them only in `StatusService.snapshot()`. If a remote
scan fails, the prior snapshot remains available with stale/error metadata.

## Idempotent run submission

The local client allocates a 64-character submission token. `submit-run`:

1. validates base/run/script/snapshot identity;
2. acquires the full-token lock and snapshot-GC lock;
3. returns an existing valid receipt if present;
4. writes a `submitting` receipt atomically;
5. calls `sbatch --parsable` at most once, with the full token in `--comment`
   and a short suffix in `--job-name`;
6. atomically records `submitted` plus the numeric job ID, or a known failure.

A timeout, lost response, or post-sbatch receipt failure is `unknown`, not a
retry signal. `reconcile-run` never submits: it checks the receipt, then Slurm
comment, then job-name suffix. Local state remains `submit_unknown` until this
produces trustworthy evidence.

With an `afterok` `EnvBinding`, submit adds
`--dependency=afterok:<build_job_id>`. A per-job invalid-dependency policy also
adds `--kill-on-invalid-dep=yes`; a declared site-wide policy omits the flag.

## Run cleanup

`clean-run` validates the owned run path and, under the submission-token lock,
removes the run directory and matching receipt. Its response always reports
`removed_run` and `removed_receipt`, including a failure after only one step.
The local service retains the run record on a partial remote result, so retry
is idempotent and does not lose the recovery handle.

## Snapshots

The local selector computes SHA-256 from the same ordered file list sent by
`rsync --files-from`. Upload targets `<base>/snapshots/<hash>/code`; only the
atomic `.complete.json` marker makes it a valid reusable snapshot. An
interrupted unmarked directory is not valid state.

`snapshot-list` derives references from valid run manifests and active run
receipts. `snapshot-gc` holds `locks/snapshot-gc.lock`, ignores unknown trees,
and selects only unreferenced complete snapshots older than 24 hours. It is a
dry run unless `--delete` is present.

## Environment identity and records

The environment ID is `<safe-name>-<hash12>` and every record stores the full
SHA-256. Scheduler resources do not affect identity. An
`envs/registry/<env_id>.json` record contains:

```text
schema_version · env_id · full_hash · backend · ownership · status
active_generation · active_prefix · current_attempt
generations[] · attempts[] · provenance · last_error · timestamps
```

References and desired-by-project flags are scan-derived view data and never
persisted in the registry.

Managed generations are immutable. An external record has no managed
generation and points at a user-owned prefix. All registry/receipt writes use
temporary files, fsync, and atomic replace. Full-hash locks serialize prepare,
promotion, cancellation, and removal.

## Environment helper operations

The helper exposes strict operations:

```text
inspect | scan | candidate-check | binding-check
prepare | verify-existing | prepare-build | build | reconcile
cancel | remove | gc
```

- `inspect` reads raw valid registry records for planning.
- `scan` batches registry, Slurm state, receipt recovery, dynamic run
  references, and scheduler warnings.
- `candidate-check` validates a locally cached candidate in one call and
  returns reuse/attach/retry/missing.
- `binding-check` validates the exact generation/prefix/attempt/job used by a
  run before submission and during status refresh.

## Managed prepare and promotion

For create/rebuild, the client uploads one attempt-specific inbox containing
the build request, source `environment.yml`, isolated channel file, generated
`CONDARC`, and `env_agent.py`. `prepare-build` then holds the full-hash lock:

1. re-read and reconcile registry/attempt receipt;
2. discard the inbox and return if a matching record is READY or active;
3. atomically move the inbox to the attempt workspace;
4. persist STAGING and receipt state;
5. start exactly one Slurm or explicitly permitted login executor.

The generation ID and final prefix are allocated before submission. Conda
builds directly into that unpublished prefix because conda prefixes are not
assumed relocatable. Each attempt has isolated config/home/package directories,
explicit channels and solver, logs, heartbeat, and provenance.

After build, the helper activation-tests the prefix, runs the project smoke
command, records explicit package URLs, and rejects any undeclared channel.
Only then does one atomic registry replacement add the generation and switch
the active pointer to READY. A failed build/smoke/promotion can never become
active; its prefix remains GC-visible staging data.

## Attempt receipts and unknown outcomes

Environment receipts advance through states such as `staged`, `submitting`,
`submitted`, `building`, `completed`, `failed`, and `cancelled`.

- A missing/`staged` receipt under the released hash lock proves interruption
  before `sbatch`; the attempt becomes `ENV_STAGING_INTERRUPTED` and is safely
  retryable.
- `submitting` without a durable job ID becomes `BUILD_UNKNOWN`; no automatic
  resubmission occurs.
- A `submitted` receipt can restore a numeric job ID to the registry.
- A login executor records host, PID, heartbeat, and completion receipt. A
  stale heartbeat/process mismatch remains `BUILD_UNKNOWN` until reconciliation.

## Environment removal and GC

Managed removal scans all run manifests below the shared base. References cause
a default refusal. After explicit force, generations are atomically moved to
`envs/trash/<env_id>/`, status becomes `REMOVING`, and detached background
deletion begins. Later scans converge to `REMOVED` or `REMOVE_UNKNOWN`.

External removal deletes only the registry entry. `gc` is dry-run by default
and reports trash plus unpublished prefixes from terminal failed attempts;
directories outside the schema-v1 layout are never candidates.
