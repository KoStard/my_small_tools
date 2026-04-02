#!/usr/bin/env python3
"""Port management CLI — find, kill, and audit listening network ports."""

import os
import re
import subprocess
from dataclasses import dataclass

import click
from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

_SIGNALS: dict[str, int] = {
    "HUP": 1, "INT": 2, "QUIT": 3, "ABRT": 6,
    "KILL": 9, "TERM": 15, "USR1": 10, "USR2": 12,
}


@dataclass
class PortEntry:
    command: str
    pid: int
    user: str
    protocol: str   # TCP or UDP
    address: str    # full address string e.g. "127.0.0.1:8080"
    port: int
    is_public: bool  # bound to 0.0.0.0, ::, or *


def _run_lsof(*args: str) -> str:
    result = subprocess.run(["lsof", "-n", "-P", *args], capture_output=True, text=True)
    return result.stdout


def _parse_lsof(output: str, protocol: str) -> list[PortEntry]:
    entries: list[PortEntry] = []
    for line in output.strip().splitlines()[1:]:  # skip header
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        command, user = parts[0], parts[2]
        name = re.sub(r'\s*\(.*\)\s*$', '', parts[8])  # strip "(LISTEN)" etc.
        m = re.search(r':(\d+)$', name)
        if not m:
            continue
        port = int(m.group(1))
        host = name[: m.start()]
        is_public = host in ("*", "0.0.0.0", "::")
        entries.append(PortEntry(command, pid, user, protocol, name, port, is_public))
    return entries


def _listening(
    protocol_filter: str = "both",
    port_range: tuple[int, int] | None = None,
) -> list[PortEntry]:
    entries: list[PortEntry] = []

    if protocol_filter in ("tcp", "both"):
        out = _run_lsof("-iTCP", "-sTCP:LISTEN")
        entries.extend(_parse_lsof(out, "TCP"))

    if protocol_filter in ("udp", "both"):
        out = _run_lsof("-iUDP")
        entries.extend(_parse_lsof(out, "UDP"))

    if port_range:
        lo, hi = port_range
        entries = [e for e in entries if lo <= e.port <= hi]

    # Deduplicate: lsof reports one row per fd; collapse by (pid, port, protocol)
    seen: set[tuple[int, int, str]] = set()
    unique: list[PortEntry] = []
    for e in entries:
        key = (e.pid, e.port, e.protocol)
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return sorted(unique, key=lambda e: e.port)


def _parse_port_spec(spec: str) -> tuple[int, int]:
    """Parse '8080' or '8080-8090' into (lo, hi)."""
    m = re.fullmatch(r'(\d+)(?:-(\d+))?', spec.strip())
    if not m:
        raise click.BadParameter(f"invalid port spec '{spec}' — use PORT or START-END")
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if not (1 <= lo <= 65535 and 1 <= hi <= 65535):
        raise click.BadParameter("ports must be in range 1–65535")
    if lo > hi:
        raise click.BadParameter(f"start port {lo} > end port {hi}")
    return lo, hi


def _parse_signal(sig: str) -> int:
    """Accept signal name (TERM, KILL, HUP, …) or number; return int."""
    name = sig.upper().removeprefix("SIG")
    if name in _SIGNALS:
        return _SIGNALS[name]
    try:
        n = int(sig)
        if 1 <= n <= 31:
            return n
    except ValueError:
        pass
    valid = ", ".join(sorted(_SIGNALS))
    raise click.BadParameter(f"unknown signal '{sig}'. Valid names: {valid}")


def _render_table(entries: list[PortEntry], title: str = "") -> None:
    if not entries:
        console.print("[dim]No matching processes found.[/dim]")
        return
    t = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("PORT", style="bold cyan", justify="right")
    t.add_column("PROTO", style="dim")
    t.add_column("ADDRESS")
    t.add_column("PID", justify="right")
    t.add_column("USER")
    t.add_column("COMMAND")
    t.add_column("PUBLIC", justify="center")
    for e in entries:
        public_label = "[bold red]YES[/bold red]" if e.is_public else "[green]no[/green]"
        t.add_row(str(e.port), e.protocol, e.address, str(e.pid), e.user, e.command, public_label)
    console.print(t)


@click.group()
def main() -> None:
    """Port management — find, kill, and audit listening ports."""


@main.command()
@click.argument("port_spec", metavar="PORT[-END]")
@click.option("--protocol", "-p", type=click.Choice(["tcp", "udp", "both"]), default="both", show_default=True)
def find(port_spec: str, protocol: str) -> None:
    """Find processes listening on PORT or a PORT-END range."""
    lo, hi = _parse_port_spec(port_spec)
    entries = _listening(protocol, (lo, hi))
    label = f"{lo}" if lo == hi else f"{lo}-{hi}"
    _render_table(entries, title=f"Listeners on port {label}")


@main.command()
@click.argument("port_spec", metavar="PORT[-END]")
@click.option("--signal", "-s", default="TERM", show_default=True,
              help="Signal name (TERM, KILL, HUP, …) or number.")
@click.option("--protocol", "-p", type=click.Choice(["tcp", "udp", "both"]), default="both", show_default=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option("--dry-run", is_flag=True, help="Show what would be killed without killing.")
def kill(port_spec: str, signal: str, protocol: str, yes: bool, dry_run: bool) -> None:
    """Kill processes listening on PORT or a PORT-END range.

    Default signal is SIGTERM. Use -s KILL for SIGKILL, -s HUP to reload, etc.
    """
    lo, hi = _parse_port_spec(port_spec)
    signum = _parse_signal(signal)
    signame = signal.upper().removeprefix("SIG")

    entries = _listening(protocol, (lo, hi))
    if not entries:
        console.print("[dim]No matching processes found.[/dim]")
        return

    label = f"{lo}" if lo == hi else f"{lo}-{hi}"
    _render_table(entries, title=f"Processes on port {label}")

    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] would send SIG{signame} to {len(entries)} process(es).")
        return

    if not yes:
        click.confirm(f"Send SIG{signame} to {len(entries)} process(es)?", abort=True)

    failed = 0
    for e in entries:
        try:
            os.kill(e.pid, signum)
            console.print(f"  [green]✓[/green] Sent SIG{signame} to PID {e.pid} ({e.command})")
        except ProcessLookupError:
            console.print(f"  [dim]– PID {e.pid} already gone[/dim]")
        except PermissionError:
            console.print(f"  [red]✗[/red] Permission denied for PID {e.pid} ({e.command})")
            failed += 1

    if failed:
        raise SystemExit(1)


@main.command("list")
@click.option("--public", is_flag=True,
              help="Show only publicly accessible ports (0.0.0.0 / :: / *) — potential exposure.")
@click.option("--protocol", "-p", type=click.Choice(["tcp", "udp", "both"]), default="both", show_default=True)
def list_ports(public: bool, protocol: str) -> None:
    """List all listening ports.

    Use --public to show only ports exposed to the network
    (bound to 0.0.0.0, ::, or *). Useful for auditing attack surface.
    """
    entries = _listening(protocol)
    if public:
        entries = [e for e in entries if e.is_public]
        title = "Publicly accessible ports (network-exposed)"
    else:
        title = "All listening ports"
    _render_table(entries, title=title)


if __name__ == "__main__":
    main()
