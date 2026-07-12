# Changelog

All notable changes to SlurmDeck are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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

[0.1.0]: https://github.com/dejay-vu/slurmdeck/releases/tag/v0.1.0
