# Sweeps and placeholders

A sweep file turns one `submit` into an array of tasks. The schema is strict
(`version: 1` required, unknown keys rejected) and has two mutually exclusive
forms.

## Matrix form

```yaml
version: 1
name: lr-sweep              # optional; seeds the run name
parameters:                 # cartesian product of all axes
  lr: [1.0e-3, 1.0e-4]
  model: [small, large]
include:                    # extra explicit combinations (optional)
  - {lr: 1.0e-3, model: xl}
exclude:                    # drop matching combinations (subset match)
  - {lr: 1.0e-4, model: large}
config:                     # per-task YAML config template (optional)
  training:
    lr: "{lr}"
    scratch: "${{SCRATCH}}/out"    # literal ${SCRATCH} via {{ }} escapes
args: ["--lr", "{lr}"]      # optional; overrides derived args
arg_style: posix            # posix | hydra | none — used when args is omitted
env:
  RUN_TAG: "{model}-lr{lr}"
```

## Explicit form

```yaml
version: 1
tasks:
  - name: baseline
    config: {training: {lr: 1.0e-3}}
    args: ["--config", "{config}"]
    env: {SEED: "1"}
  - config: {training: {lr: 1.0e-4}}   # name defaults to task-<index>
```

## Derived args (`arg_style`)

When `args` is omitted and a `config` exists, args are derived from the
resolved config tree:

- `posix`: `{training: {lr: 0.1, amp: true, debug: false}}` →
  `--training-lr 0.1 --training-amp --no-training-debug`; lists expand to
  repeated values; `null` values are skipped.
- `hydra`: `training.lr=0.1 training.amp=true`; `null` renders as `null`
  explicitly; lists render as `[a,b]`.
- `none`: no derived args.

## Placeholder semantics

One grammar everywhere: `{name}` where `name` is an identifier. Literal braces
are `{{` and `}}`. Any other unescaped `{` is an error at submit time, and an
unknown name fails with the list of names valid at that position.

Contexts (all resolved locally at submit time):

| Position                | Available names                                                        |
| ----------------------- | ---------------------------------------------------------------------- |
| `config` values         | sweep params, `index`, `task_id`, `task_name`, `run`                    |
| `args`, `env` values    | the above + `config`, `output`                                          |
| the command             | the above + `{args}`                                                    |

Rules worth knowing:

- In a config value, an **exact** `"{lr}"` keeps the parameter's type
  (float/int/bool); embedded use (`"run-{lr}"`) renders text
  (booleans → `true`/`false`, `null` → empty).
- In argv commands, `{args}` must be a standalone token and splices task args
  as separate argv entries; other placeholders may be embedded in tokens
  (`--config={config}` works).
- In `--shell` commands, substitution is quote-aware: a placeholder outside
  quotes becomes a fully quoted word; inside `"…"` or `'…'` it is escaped for
  that quoting style — `--config {config}` and `--config "{config}"` both
  produce one correct argument even for paths with spaces.
- If the sweep produces args but the command never uses `{args}`, submit fails
  (your parameters can never be silently dropped).

Validate and inspect before submitting:

```bash
slurmdeck sweep validate sweep.yaml
slurmdeck sweep preview sweep.yaml --limit 5 [--json]
```
