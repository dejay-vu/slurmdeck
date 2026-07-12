# The SlurmDeck TUI

`slurmdeck ui` opens the Textual interface over the same services, status
views, operation events, recovery rules, and semantic Rich theme as the CLI.
CLI and TUI may be used interchangeably.

```bash
cd my-project                       # optional; otherwise opens Remotes
slurmdeck ui
slurmdeck ui --theme monokai        # or any theme listed by Theme
```

Selecting **Theme** from the command palette offers the full Textual theme set,
including Monokai, Nord, Dracula, Catppuccin, and the SlurmDeck dark/light/mono
themes. The selection is saved as the user default for later sessions.
`SLURMDECK_THEME` and `ui --theme` temporarily override the saved value;
`NO_COLOR` always forces the mono/plain contract and disables switching for
that session. CLI semantic colors follow the saved theme's dark/light mode.

## Screen map

Top-level modes use `1`, `2`, and `3`:

```text
Runs (1)                    Environments (2)             Remotes (3)
├─ New Run form             ├─ prepare / attach          ├─ Add Remote form
├─ run/task detail          ├─ environment detail        ├─ Profile form
└─ task logs                ├─ environment logs          └─ Doctor detail
                            └─ cancel/rebuild/remove/GC
```

At 100 columns and wider, Runs, Environments, Remotes, Doctor, and Logs use a
list/detail split. At compact widths (including the supported 80-column
layout), the list remains usable and Enter opens a dedicated detail page.
Identifiers, timestamps, prefixes, reasons, and log paths are not shortened in
detail views.

The footer shows only the highest-priority action for the current screen;
secondary actions remain available through keys and help. This keeps 80-column
screens stable instead of dropping fields to make room for every shortcut.

## Global keys

| Key | Action |
| --- | --- |
| `1` / `2` / `3` | Runs / Environments / Remotes |
| `r` | refresh the current remote-backed view |
| `:` | command palette and auto-refresh toggle |
| `?` | contextual keyboard/workflow help |
| `ctrl+x` | dismiss the persistent error panel |
| `escape` | clear filter, close modal, or go back |
| `ctrl+c` twice | quit; the second press must arrive within two seconds |

`Ctrl+Q` is deliberately unbound so it cannot trigger Textual's default
single-key exit. `Ctrl+C` remains available for copying selected text in input
and text-area widgets; outside those widgets, the first press asks for exit
confirmation and the second press exits.

## Runs

| Key | Action |
| --- | --- |
| `n` | open New Run |
| `enter` / `o` | open selected run/detail |
| `s` | submit a planned run |
| `c` | cancel the Slurm run |
| `t` | retry failed tasks as a new immutable run |
| `p` | pull results |
| `d` | clean local + remote state |
| `/` | filter ID/name/state/reason |

New Run accepts a shell-like command line (parsed to argv), an optional
existing Sweep YAML path, name, all resource overrides, READY/afterok policy,
and a “Submit immediately” choice. It does not introduce a second RunPlan YAML
schema. `afterok` appears only for an active Slurm environment build whose
effective profile guarantees invalid-dependency termination.

Run detail uses `l` for selected task logs and `f` to cycle all/active/failed.
Task log keys are `f` follow, `tab` stdout/stderr, `w` wrap, and `r` reload.

An empty pull becomes an actionable error rather than a false success. Partial
pull and clean follow the same outcome contracts as the CLI; a partial clean
retains the local run so the user can retry.

## Environments

| Key | Action |
| --- | --- |
| `p` | prepare the project's configured environment |
| `a` | attach to the project's active attempt |
| `l` | open persisted attempt logs |
| `c` | cancel only the active environment attempt |
| `b` | build a new immutable generation |
| `d` | remove/unregister the selected environment |
| `g` | dry-run GC, then confirm exact candidates |

The list marks the environment desired by the current project. Detail shows
backend/ownership, full hash, status, scheduler job/reason, resources,
generation/prefix, attempt, stdout/stderr, references, and last error.

Prepare and rebuild always use `.slurmdeck/project.yaml` plus the selected
Remote's ClusterProfile. Active attempts attach rather than submit duplicates.
Managed removal is blocked by run references; external removal never deletes
the user prefix. GC previews count/bytes first and excludes directories outside
the schema-v1 layout.

Environment logs use the same follow/stream/wrap/reload keys as task logs.

## Remotes, ClusterProfile, and Doctor

| Key | Action |
| --- | --- |
| `a` | Add Remote |
| `e` | edit/import the selected ClusterProfile |
| `u` | select remote |
| `c` / `x` | connect / disconnect |
| `d` | run read-only Doctor |

Add Remote explicitly chooses a direct `user@login` destination or an SSH
config alias, plus the remote base and whether to select it immediately.

The Profile form covers executor/login policy, shared filesystem, module
initialization, conda/modules, network/channels/mirrors, Slurm defaults,
afterok/invalid-dependency policy, and platform. It can import a strict YAML
file, preview the complete diff, and only writes after “Save profile”.

Doctor is a separate read-only diagnosis. It has no Apply or Save action and
never mutates Remote YAML, cache, project/database files, or remote paths.

## Feedback, concurrency, and stale state

- Remote work runs in thread workers; the screen paints cached data or an
  explicit Loading state before waiting on SSH.
- Read-only workers can overlap. Mutations share one exclusive group so two
  submissions/removals cannot race through the UI.
- The status bar shows the selected operation phase/message and elapsed time.
  Short operations do not flash a spinner.
- Refresh is every 15 seconds while submitted runs exist, every 60 seconds
  while cancellation artifacts settle, and paused when idle. Manual `r`
  remains available.
- A refresh failure with a prior snapshot keeps the table visible, marks it
  stale, and records last success/failure. It does not replace useful data with
  an empty screen.
- Errors appear both as a notification and in a persistent dismissible panel.
  Repeated identical refresh errors are throttled.
- Tables update by stable row key, preserving selection and scroll position.

## Confirmation and safety

Destructive actions use an explicit confirmation modal whose safe default is
No. Environment cancel does not cancel dependent runs automatically, and run
cancel does not cancel a shared environment build. Rebuild creates a new
generation; existing run bindings do not move.

The TUI never expands Doctor findings into saved policy. Site permissions such
as login builds and site-wide invalid-dependency termination must be declared
by the user through the Profile form or CLI profile file.
