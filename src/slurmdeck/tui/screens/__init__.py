"""TUI screens (one module per screen, all built on DeckScreen)."""

from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.screens.doctor import DoctorScreen
from slurmdeck.tui.screens.env_logs import EnvLogsScreen
from slurmdeck.tui.screens.envs import EnvsScreen
from slurmdeck.tui.screens.help import HelpScreen
from slurmdeck.tui.screens.logs import LogsScreen
from slurmdeck.tui.screens.record_detail import RecordDetailScreen
from slurmdeck.tui.screens.remotes import RemotesScreen
from slurmdeck.tui.screens.run_detail import RunDetailScreen
from slurmdeck.tui.screens.runs import RunsScreen

__all__ = [
    "DeckScreen",
    "DoctorScreen",
    "EnvLogsScreen",
    "EnvsScreen",
    "HelpScreen",
    "LogsScreen",
    "RecordDetailScreen",
    "RemotesScreen",
    "RunDetailScreen",
    "RunsScreen",
]
