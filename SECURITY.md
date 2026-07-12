# Security Policy

## Supported versions

Security fixes are provided for the latest published SlurmDeck release only.

## Reporting a vulnerability

Please do not open a public issue or discussion for a suspected vulnerability.
Use [GitHub private vulnerability reporting](https://github.com/dejay-vu/slurmdeck/security/advisories/new).

Include the affected version, impact, reproduction steps, and any suggested
remediation. Remove credentials, personal data, cluster hostnames, and other
sensitive information from the report unless they are essential to explain the
issue.

The maintainer will investigate the report and coordinate a fix and disclosure
as soon as practical. Please allow a reasonable opportunity for remediation
before publishing details. If private reporting is unavailable, open an issue
requesting a private contact channel without including vulnerability details.

## Scope

Security-sensitive areas include SSH host verification, snapshot file
selection, remote path validation, command construction, local state
permissions, submission idempotency, and cleanup boundaries. Reports about a
third-party dependency should identify both the dependency and the SlurmDeck
workflow that is affected.
