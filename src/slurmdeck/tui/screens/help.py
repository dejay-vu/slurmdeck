"""In-app help overlay: workflow crib plus every keybinding, grouped by screen."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

_HELP = """\
[bold]SlurmDeck — keyboard reference[/bold]

[bold]Workflow[/bold]  [dim]remote add → connect → init → env prepare → submit → monitor → pull[/dim]

[ui.accent]Global[/ui.accent]
  1 / 2 / 3      switch section: Runs / Environments / Remotes
  r              refresh (status on Runs, listings elsewhere)
  /              filter or search the current list
  :              command palette
  ?              this help
  escape         back / close
  ctrl+c twice   quit (second press within 2 seconds)

[ui.accent]Runs[/ui.accent]
  n              create a run from command / sweep YAML / resources
  enter          inspect inline (wide) / open details (compact)
  o              open the task and log workflow
  s              submit a planned run
  c              cancel the selected run
  t              retry its failed tasks (new run)
  p              pull results to a local directory
  d              clean the run (local record + remote directory)

[ui.accent]Run details[/ui.accent]
  enter / l      open the selected task's logs
  f              cycle task filter: all → active → failed
  c / t / p      cancel / retry / pull this run

[ui.accent]Logs[/ui.accent]
  f              follow (tail -F) on/off
  tab            switch stdout / stderr
  w              toggle line wrapping
  r              reload the tail

[ui.accent]Environments[/ui.accent]
  p              prepare the project environment on the remote
  a / l          attach to an active build / open build logs
  c / b          cancel active build / create a new generation
  d              remove the selected environment
  g              preview safe garbage collection, then confirm deletion

[ui.accent]Remotes[/ui.accent]
  a / e          add a remote / edit its explicit ClusterProfile
  enter / u      inspect the selected remote / make it current
  c / x          connect / disconnect
  d              run doctor checks

[dim]Destructive actions always ask for confirmation. One remote operation runs
at a time; its progress is shown in the status bar. Status refreshes happen
automatically while runs are active and pause when everything is settled.[/dim]

[dim]Ctrl+Q is intentionally unbound. Ctrl+C still copies selected text in
inputs and text areas; otherwise press it twice to exit.[/dim]

[dim]The full Theme list (including Monokai, Nord, and Dracula) is saved for
later sessions. Command-line and environment overrides remain temporary.[/dim]
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar = [
        Binding("escape", "dismiss_help", "Close"),
        Binding("q", "dismiss_help", "Close", show=False),
        Binding("question_mark", "dismiss_help", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-panel"):
            # Rich owns semantic style resolution; Textual's string-markup
            # parser does not accept dotted theme role names.
            yield Static(Text.from_markup(_HELP))

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
