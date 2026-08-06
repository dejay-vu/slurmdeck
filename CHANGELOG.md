# Changelog

All notable changes to SlurmDeck are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## [0.4.0] - 2026-08-06

### Added

- Per-remote `agent_python` configuration and `remote add --agent-python`, so
  clusters whose default `python3` is too old can use a shared newer
  interpreter for SSH helpers, environment helpers, and Slurm run agents.

### Changed

- Environment guidance now distinguishes managed dependency installation from
  project source snapshots and no longer recommends checkout-dependent
  `pip install -e .` commands during remote environment builds.

### Fixed

- Conda activation now applies package variables from `etc/conda/env_vars.d`
  and environment variables from `conda-meta/state` before `activate.d`
  scripts, including Conda's non-overriding `***unset***` marker semantics.
- Default environment log reads now fall back to the most recent attempt that
  created logs when a later cancelled attempt never started; explicit stream
  selection and log following remain pinned to the current/latest attempt.
- `env status` now marks the project-desired environment consistently with
  `env list` and `env show`.
- Global TUI refresh skips settled cancelled runs whose remote registration was
  removed, while still reconciling cancelled runs with active or unknown tasks.

## [0.3.0] - 2026-07-29

### Added

- Project-scoped named targets that atomically bind a remote, resources, and
  environment while retaining schema-v1 legacy single-target configuration.
- `target list`, `target show`, and `target use`, plus per-operation
  `--target` selection for submission, environment workflows, and Doctor.

### Changed

- The local project SQLite database is schema version 2 and records each run's
  selected target. Schema-1 databases migrate automatically under a write lock;
  concurrent opens and an already-added `target` column are handled safely.
  Releases limited to database schema 1 cannot reopen a migrated project.
- Doctor remains read-only for old databases and now recommends the automatic
  migration command instead of replacing project state. In legacy projects,
  `doctor --remote` still checks the top-level environment; in target projects
  it is an explicit remote-only diagnostic.
- Retry preserves the source run's target, remote, resources, exact environment
  binding, and activation script; legacy runs are not relabeled after a project
  migrates to named targets.

## [0.2.0] - 2026-07-29

### Added

- Slurm reservation support in project resources, per-submit CLI overrides,
  run and managed-environment `sbatch` rendering, and the TUI run form.

## [0.1.0] - 2026-07-12

Initial public release.

### Added

- Rich command-line and Textual interfaces for remote, run, environment,
  snapshot, sweep, log, status, and result workflows.
- SSH remote registration using direct destinations or OpenSSH aliases, with
  explicit host-key policies and ControlMaster connection reuse.
- Immutable run materialization, parameter sweeps, Slurm array submission,
  status reconciliation, retry, cancellation, result pulling, and cleanup.
- Content-addressed project snapshots with preview, reuse, reference-aware
  garbage collection, and sensitive-file protection.
- Managed conda environments with immutable generations, explicit cluster
  policy, Slurm or permitted login-node execution, and channel verification.
- Registration of existing environments without taking ownership of their
  files.
- Stable machine-readable JSON output and responsive terminal layouts.

### Security

- Local state directories and files use private permissions.
- Snapshot selection blocks common credential files and private-key content by
  default.
- SSH host-key behavior inherits the user's OpenSSH policy unless explicitly
  overridden.
- Submission receipts and locks prevent automatic duplicate submission after
  uncertain remote outcomes.

[Unreleased]: https://github.com/dejay-vu/slurmdeck/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/dejay-vu/slurmdeck/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dejay-vu/slurmdeck/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dejay-vu/slurmdeck/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dejay-vu/slurmdeck/releases/tag/v0.1.0
