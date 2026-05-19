"""Rich-based screens for the test harness.

`pick_fixture` is the questionary-based picker shown at startup.
`RunState` + `render_state` produce the live panel that updates while the
round-trip is in flight. `render_final` prints the final result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console, Group
from rich.json import JSON
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

console = Console()


class Stage(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Fixture:
    ticker: str
    date: str           # YYYY-MM-DD or "(custom)"
    summary: str
    source: str         # path or "(pasted)"


@dataclass
class RunState:
    title: str = ""
    stages: dict[str, Stage] = field(default_factory=dict)
    webhook_id: str = ""
    handler_status: int | None = None
    captured_body: dict | None = None
    rejection: dict | None = None

    def add(self, label: str) -> None:
        self.stages[label] = Stage.PENDING

    def set(self, label: str, stage: Stage) -> None:
        self.stages[label] = stage


def pick_fixture(fixtures_dir: Path) -> Fixture:
    """Show the picker, return the chosen fixture."""
    builtins = sorted(fixtures_dir.glob("*.json"))
    choices: list[Any] = []
    for path in builtins:
        data = json.loads(path.read_text())
        date = path.stem.split("_", 1)[1] if "_" in path.stem else "(unknown)"
        choices.append(
            questionary.Choice(
                title=f"{data['ticker']:<5}  · earnings · {date}",
                value=("builtin", path),
            )
        )
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title="Paste a path to a JSON file", value=("path", None)))
    choices.append(questionary.Choice(title="Paste raw JSON", value=("raw", None)))

    answer = questionary.select(
        "Pick a test event:",
        choices=choices,
        use_arrow_keys=True,
    ).unsafe_ask()

    kind, val = answer
    if kind == "builtin":
        data = json.loads(val.read_text())
        date = val.stem.split("_", 1)[1] if "_" in val.stem else "(unknown)"
        return Fixture(ticker=data["ticker"], date=date, summary=data["summary"], source=str(val))
    if kind == "path":
        path_str = questionary.path("Path to fixture JSON:").unsafe_ask()
        path = Path(path_str).expanduser()
        data = json.loads(path.read_text())
        date = path.stem.split("_", 1)[1] if "_" in path.stem else "(custom)"
        return Fixture(ticker=data["ticker"], date=date, summary=data["summary"], source=str(path))
    raw = questionary.text(
        "Paste raw JSON (single line):",
        multiline=True,
    ).unsafe_ask()
    data = json.loads(raw)
    return Fixture(ticker=data["ticker"], date="(custom)", summary=data["summary"], source="(pasted)")


def render_state(state: RunState) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=2)
    table.add_column()
    for label, stage in state.stages.items():
        if stage is Stage.ACTIVE:
            marker: Any = Spinner("dots", style="cyan")
            text = Text(label, style="cyan")
        elif stage is Stage.DONE:
            marker = Text("✓", style="green")
            text = Text(label)
        elif stage is Stage.FAILED:
            marker = Text("✗", style="red bold")
            text = Text(label, style="red")
        else:
            marker = Text("○", style="dim")
            text = Text(label, style="dim")
        table.add_row(marker, text)

    parts: list[Any] = []
    if state.webhook_id:
        parts.append(Text(f"webhook_id  {state.webhook_id}", style="dim"))
        parts.append(Text(""))
    parts.append(table)
    return Panel(Group(*parts), title=state.title, border_style="cyan", padding=(1, 2))


def render_final(state: RunState) -> None:
    ok = state.rejection is None and state.captured_body is not None
    border = "green" if ok else "red"

    parts: list[Any] = []
    if state.webhook_id:
        parts.append(Text(f"webhook_id  {state.webhook_id}", style="dim"))
        parts.append(Text(""))

    table = Table.grid(padding=(0, 1))
    table.add_column(width=2)
    table.add_column()
    for label, stage in state.stages.items():
        if stage is Stage.DONE:
            table.add_row(Text("✓", style="green"), Text(label))
        elif stage is Stage.FAILED:
            table.add_row(Text("✗", style="red bold"), Text(label, style="red"))
        elif stage is Stage.ACTIVE:
            table.add_row(Text("…", style="yellow"), Text(label, style="yellow"))
        else:
            table.add_row(Text("○", style="dim"), Text(label, style="dim"))
    parts.append(table)

    if state.captured_body:
        parts.append(Text(""))
        parts.append(Text("Captured body", style="bold"))
        parts.append(JSON.from_data(state.captured_body))

    if state.rejection:
        parts.append(Text(""))
        parts.append(Text("Rejection", style="bold red"))
        parts.append(JSON.from_data(state.rejection))

    parts.append(Text(""))
    parts.append(Text("All systems green." if ok else "Submission rejected — see above.", style=border + " bold"))

    console.print(Panel(Group(*parts), title=state.title, border_style=border, padding=(1, 2)))
