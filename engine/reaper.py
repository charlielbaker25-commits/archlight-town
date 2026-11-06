"""Reaper-style status lines for the terminal: terse, text only, numbers in plain sight.

Clarity beats character (BLUEPRINT 8.3): the personality is a short prefix and
clipped phrasing, never a replacement for the facts.
"""
from rich.console import Console
from rich.markup import escape

console = Console(highlight=False)


def report(message: str) -> None:
    console.print(f"[bold cyan]REAPER[/] {escape(message)}")


def warn(message: str) -> None:
    console.print(f"[bold yellow]REAPER[/] [yellow]{escape(message)}[/]")


def alert(message: str) -> None:
    console.print(f"[bold red]REAPER[/] [red]{escape(message)}[/]")


def plain(message: str = "", soft_wrap: bool = False) -> None:
    """soft_wrap=True never breaks the line (a link stays in one piece, so it can be copied)."""
    console.print(escape(message), soft_wrap=soft_wrap)
