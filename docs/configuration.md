# Configuration

SlurmDeck `0.1.0` uses strict schema-v1 YAML. Unknown keys are errors and
validation identifies the file and field. Only schema version 1 is supported.

## User state

On Linux, user state normally lives below
`$XDG_CONFIG_HOME/slurmdeck` (usually `~/.config/slurmdeck`):

```text
state.yaml                         selected remote name and saved UI theme
remotes/<name>.yaml                Remote plus optional ClusterProfile
cache/environments/<name>.yaml     advisory observation/registry cache
```

SlurmDeck creates its local state directories with mode `0700` and YAML,
SQLite, WAL, and SHM files with mode `0600`. A mutating write or database open
also tightens an existing path that is more permissive. Read-only diagnostics
do not change filesystem metadata.

The environment cache is bound to the remote name and resolved base. Cluster
observations expire after 24 hours; corrupt, stale, mismatched, or unwritable
cache entries are ignored. Remote registry/helper results remain authoritative.
Doctor and `env plan` never update this cache.

## Remote and ClusterProfile

`slurmdeck remote add` writes the connection fields. Exactly one of `host` and
`ssh_alias` is required. `remote connect` expands the base on the remote,
creates the schema-v1 top-level layout, atomically stores `resolved_base`, and
may cache a read-only capability observation.

```yaml
name: hpc
host: user@login.example.com       # XOR ssh_alias: cluster-alias
base: $DATA/slurmdeck              # $VARS and ~ expand remotely
host_key_policy: inherit           # inherit | strict | accept-new
resolved_base: /data/user/slurmdeck
```

The default `host_key_policy: inherit` does not pass a
`StrictHostKeyChecking` override, so the user's `~/.ssh/config` and system
OpenSSH policy remain authoritative. Because SlurmDeck runs SSH
non-interactively, an unknown host that would normally prompt must first be
verified with ordinary `ssh <destination>`, or configured in OpenSSH.

`strict` forces `StrictHostKeyChecking=yes` and therefore requires a matching
entry in `known_hosts`. `accept-new` is an explicit trust-on-first-use choice:
OpenSSH adds a new key automatically but still rejects a changed key. Confirm
the host fingerprint through a trusted channel before choosing it. SlurmDeck
never disables changed-key checks.

Connection details do not authorize software builds. Managed prepare requires
an explicit `cluster` policy, stored in the same file and shared by every
project selecting the remote:

```yaml
name: hpc
ssh_alias: cluster-alias
base: $DATA/slurmdeck
resolved_base: /data/user/slurmdeck
cluster:
  schema_version: 1
  allowed_build_executors: [slurm]
  default_build_executor: slurm
  login_build_policy: forbidden
  shared_filesystem:
    login_to_compute: true
  module_initialization:
    strategy: commands             # none | source | commands
    commands:
      - source /etc/profile.d/modules.sh
  conda:
    executable: conda
    modules: ["<site-conda-module>"]
  network:
    compute_access: restricted     # full | restricted | none
    channel_access: direct         # direct | mirrors | none
    mirrors: []                    # required only for channel_access: mirrors
  slurm:
    partition: "<environment-build-partition>"
    account: "<account>"
    qos: "<qos>"
    constraint: null
    afterok_dependency: true
    kill_invalid_dependency: per_job  # per_job | site_wide | unsupported
  platform:
    system: Linux
    machine: x86_64
    conda_subdir: linux-64
```

Replace every angle-bracket placeholder with site-approved values. The profile
can be incomplete while it is being diagnosed, but managed `env prepare`
requires a complete effective contract. Policy values are never inferred from
an observed executable:

- `allowed_build_executors` declares permitted placement. `slurm` is the safe
  default when sustained work is not allowed on login nodes.
- `login_build_policy: forbidden` prevents login-node builds. Set `allowed`
  only when site documentation explicitly permits sustained software builds on
  login nodes; then include `login` in `allowed_build_executors`.
- `shared_filesystem.login_to_compute` must state whether the remote base and
  generation prefix are visible on compute nodes.
- `module_initialization` is explicit because build/runtime activation uses
  `bash --noprofile --norc`, not a login shell.
- `network` states compute-node and channel reachability. Mirrors must be
  declared; SlurmDeck does not silently switch channels.
- `afterok_dependency` plus `kill_invalid_dependency` gates scheduler-ordered
  run submission. Use `per_job` only if `--kill-on-invalid-dep=yes` is supported;
  use `site_wide` only for a documented equivalent guarantee.
- `platform` prevents a login/compute architecture mismatch from producing an
  unusable conda generation.

Save and inspect the profile explicitly:

```bash
slurmdeck remote profile set hpc --file cluster-profile.yaml
slurmdeck remote profile show hpc
slurmdeck doctor --remote hpc
```

`profile set` validates before atomic replacement. `profile show` and Doctor
are read-only; Doctor never generates, applies, or saves policy.

## Project — `.slurmdeck/project.yaml`

`slurmdeck init` generates `project_id` and `display_name`:

```yaml
schema_version: 1
project_id: 4d3c39c8-1f44-44ce-ae77-a1f295f2fdca
display_name: my-project
remote: hpc                       # optional project pin
resources:
  time: "12:00:00"
  cpus: 1
  mem: 8G
  gres: gpu:1
  partition: gpu
  account: myaccount
  qos: normal
  constraint: a100
  max_parallel: 8
env:                              # optional; managed example
  type: conda
  name: ml
  spec_file: environment.yml
  modules: [cuda/12.1]
  post_install: ["pip install -e ."]
  smoke_test: "python -c 'import torch'"
  channel_priority: strict        # strict | flexible | disabled
  solver: libmamba                # libmamba | classic
  build_resources:               # field-wise overlay on resources
    time: "01:00:00"
    cpus: 2
    mem: 8G
    partition: short
sync:
  include_untracked: false
  ignore_file: .slurmdeckignore
  extra_ignores: ["data/", "*.ckpt"]
  allow_sensitive_files: []        # exact reviewed paths only; normally keep empty
```

CLI resource options overlay `resources` for one run. Environment build
resources start from the project resources and apply only the fields present
in `env.build_resources`; scheduler resources are attempt provenance and do
not change environment identity.

Snapshots always exclude `.git`, `.slurmdeck`, `pulled/`, caches, and common
build artifacts. Ignore rules are directory-aware and top-down: excluding
`data/` prevents traversal into the directory, not merely selection of its
files. Before submission, `slurmdeck snapshot preview` shows the exact local
file set and content hash.

SlurmDeck refuses common secret-bearing files such as `.env`, private keys,
credential stores, and files containing a private-key header. Exclude them
with `.slurmdeckignore`. If a sensitive-looking file is genuinely required,
review it first and add its exact project-relative path to
`sync.allow_sensitive_files`; broad patterns and parent traversal are not
accepted. This guard is intentionally narrow and does not replace a secret
scanner.

## Environment specifications

### Managed conda

The `environment.yml` bytes are part of the identity, and its `channels` list
is the only channel declaration source:

```yaml
name: ml
channels:
  - conda-forge
  - nodefaults
dependencies:
  - python=3.12
  - numpy
```

The ID is `<safe-name>-<hash12>` and the full SHA-256 is retained. The digest
covers backend intent, spec bytes, modules, post-install, smoke, channel
priority, and solver. Each attempt receives its own `CONDARC`, home/config and
package directories. SlurmDeck records explicit package URLs, rejects URLs
outside the declared channels, and never accepts channel Terms of Service.

`--rebuild` creates a new attempt and immutable generation. There is no
`--force` alias and existing runs continue to reference their exact generation.

### External prefix

```yaml
env:
  type: existing
  name: shared-pytorch
  prefix: /shared/conda/envs/pytorch
  modules: [cuda/12.1]
  smoke_test: "python -c 'import torch'"
```

SlurmDeck activation-probes and registers the prefix but never owns or deletes
it. `env remove` unregisters an external environment only.

## Machine-managed local state

```text
.slurmdeck/project.yaml
.slurmdeck/slurmdeck.db                 SQLite schema v1, WAL for mutations
.slurmdeck/runs/<run_id>/               committed materialized runs
.slurmdeck/staging/runs/<run_id>/...    crash-recoverable temporary commits
.slurmdeck/locks/run-materialization/   local run commit locks
```

Read-only Doctor opens SQLite with `mode=ro&immutable=1` and does not create a
database, WAL, or SHM file.

## Machine-managed remote state

```text
runs/<run_id>/
snapshots/<sha256>/
envs/registry/<env_id>.json
envs/generations/<env_id>/<generation_id>/
envs/attempts/<env_id>/<attempt_id>/
envs/inbox/<attempt_id>/
envs/trash/<env_id>/
locks/run/<submission_token>.lock
locks/env/<full_hash>/.lock
receipts/run/<submission_token>.json
receipts/env/<attempt_id>.json
```

See [remote-protocol.md](remote-protocol.md) for contracts and lifecycle rules.
Directories outside this layout are ignored by the registry and never deleted
by schema-v1 GC.
