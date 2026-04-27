"""Shared Rich console + panel helpers for human-readable terminal output."""

from __future__ import annotations

from typing import Any, Mapping

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console: Console = Console(highlight=False, log_time=True, log_path=False)


_ICONS: dict[str, str] = {
    "call_start": "📞",
    "call_end": "🏁",
    "twilio": "🔌",
    "gemini": "🤖",
    "tool": "🛠️",
    "ticket": "🎫",
    "escalation": "🚨",
    "db": "🗄️",
    "agent": "🗣️",
    "customer": "👤",
    "info": "ℹ️",
    "debug": "🔍",
    "error": "❌",
    "warn": "⚠️",
}


def render_event_panel(
    title: str,
    fields: Mapping[str, Any],
    *,
    style: str = "cyan",
    icon_key: str | None = None,
) -> None:
    """Render a labelled key/value panel for a lifecycle event."""
    icon = _ICONS.get(icon_key or "", "")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for key, value in fields.items():
        table.add_row(str(key), str(value if value is not None else "-"))

    header = f"{icon} {title}".strip()
    console.print(Panel(table, title=header, border_style=style, expand=False))


def render_message(text: str, *, style: str = "white") -> None:
    """Print a free-form message line."""
    console.print(Text(text, style=style))


def render_error_panel(title: str, message: str, fields: Mapping[str, Any] | None = None) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold red")
    body.add_column()
    body.add_row("error", message)
    for key, value in (fields or {}).items():
        body.add_row(str(key), str(value))
    console.print(Panel(body, title=f"❌ {title}", border_style="red", expand=False))
