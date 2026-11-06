"""The terminal chat with Reaper (`town reaper`): Charlie types, Reaper answers. Also the backup to the page.

History lasts for this session only. Each turn is logged by the brain (engine/jarvis.py).
Confirmation is read here, straight from Charlie's keyboard: after Reaper shows a list of decisions, ONLY
typing `y` (or `yes`) at the "Confirm?" prompt carries it out. Anything else cancels it. The model's words
never reach this prompt, so nothing Reaper says can count as a confirmation.
"""
from typing import Any, Callable

from rich.console import Console
from rich.markup import escape

from engine import decisions as dec
from engine.jarvis import Reaper
from engine.signature import after_batch

EXIT_WORDS = frozenset({"exit", "quit", "bye", "q"})
YES = frozenset({"y", "yes"})
BANNER = ("Reaper online. Type to talk. 'status' and 'queue' are free. 'stand down' halts everything. "
          "'exit' to leave.")


def run_chat(brain: Reaper, can_decide: bool, console: Console,
             read: Callable[[str], str] | None = None) -> None:
    read = read or (lambda prompt: console.input(prompt))
    console.print(f"[bold cyan]REAPER[/] {escape(BANNER)}")
    while True:
        try:
            said = read("[bold]CHARLIE>[/] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if said.strip().lower() in EXIT_WORDS:
            break
        if not said.strip():
            continue
        result = brain.turn(said)
        style = "bold red" if result.mode in ("stand_down", "stopped") else "bold cyan"
        console.print(f"[{style}]REAPER[/] {escape(result.text)}")
        cost = f"${result.cost_usd:.5f} on {result.model}" if result.model else "no model call"
        console.print(f"[dim]  ({result.mode}, {result.seconds:.1f}s, {cost})[/]")
        if result.proposal is not None and not result.proposal.empty:
            brain.note(_confirm(brain.town, result.proposal, can_decide, console, read, brain.rules))
    console.print("[bold cyan]REAPER[/] Session closed.")


def _confirm(town: Any, proposal: dec.Proposal, can_decide: bool, console: Console,
             read: Callable[[str], str], rules: dec.Rules = dec.Rules()) -> str:
    """Asks Charlie, then returns what happened (for Reaper's history)."""
    try:
        answer = read("[bold yellow]Confirm? Type y to carry out exactly the list above; anything else cancels:[/] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in YES:
        dec.cancel(town, proposal)
        console.print("[bold cyan]REAPER[/] Cancelled. Nothing done.")
        return "Charlie cancelled the proposed decisions. Nothing was done."
    try:
        lines = dec.confirm(town, proposal, can_decide, rules)
    except dec.DecisionError as err:
        console.print(f"[bold red]REAPER[/] {escape(str(err))}")
        return f"The confirmation didn't go ahead: {err}"
    for line in lines:
        console.print(f"[bold cyan]REAPER[/] {escape(line)}")
    signature = after_batch(lines, proposal.count)   # a big batch, all done: Reaper's signature line
    if signature:
        console.print(f"[bold cyan]REAPER[/] {escape(signature)}")
    return "Charlie confirmed. Results: " + " ".join(lines)
