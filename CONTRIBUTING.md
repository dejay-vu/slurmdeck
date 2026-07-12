# Contributing to SlurmDeck

Thanks for helping improve SlurmDeck. Bug reports, documentation fixes, tests,
and focused code changes are welcome.

## Before starting

- Search existing issues before opening a new one.
- Open an issue before a large behavioral or data-format change so the design
  and compatibility impact can be discussed first.
- Report security vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md).

## Development setup

Use Python 3.11 or newer on a POSIX system. Git is required; Bash and rsync are
needed for the complete end-to-end test suite.

```bash
git clone https://github.com/dejay-vu/slurmdeck.git
cd slurmdeck
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pre-commit install --hook-type commit-msg
```

Most tests use local fakes and do not require access to a Slurm cluster.

## Commit messages

Every commit must follow
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):

```text
<type>[optional scope]: <description>
```

Use one of these types:

- `feat`: user-visible functionality
- `fix`: a user-visible bug fix
- `bump`: a release version update
- `docs`: documentation only
- `test`: tests only
- `refactor`: code changes that neither add functionality nor fix a bug
- `perf`: performance improvements
- `build`: packaging or build-system changes
- `ci`: continuous-integration changes
- `chore`: maintenance that does not fit another type
- `style`: formatting or other non-functional source changes
- `revert`: a reverted commit

Scopes are optional; concise scopes such as `cli`, `tui`, `remote`, or `env`
are useful when they add context. Keep the first line at or below 72 characters.
Use `!` before the colon and a `BREAKING CHANGE:` footer when a commit makes a
breaking change.

Examples:

```text
feat(tui): add job detail view
fix(remote): handle connection timeout
docs: improve installation guide
```

The installed `commit-msg` hook checks this format locally. CI checks every
commit again, so bypassing the local hook does not bypass the project rule.
`cz commit` can be used for an interactive commit prompt.

## Making changes

- Keep changes focused and add tests for observable behavior.
- Update README or `docs/` when commands, configuration, safety behavior, or
  user workflows change.
- Do not commit credentials, private keys, cluster-specific details,
  `.slurmdeck/` state, generated outputs, or local development instructions.
- Preserve read-only behavior for diagnostic and planning commands.
- Preserve explicit confirmation and dry-run defaults for destructive actions.

Run the complete local checks before submitting a pull request:

```bash
ruff format .
ruff check .
mypy
python -m pytest -q
```

CI tests every supported Python version and also builds, validates, and
installs the wheel and source distribution.

## Pull requests

Use a Conventional Commit-formatted pull request title. SlurmDeck uses squash
merges, so the pull request title becomes the commit subject on `main`.

Describe the user-visible change, safety implications, and checks performed.
Keep unrelated refactoring out of the same pull request. Review feedback may
request smaller commits or additional failure-path coverage.

By submitting a contribution, you agree that it may be distributed under the
project's MIT License.
