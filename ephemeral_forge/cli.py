"""CLI entry point — typer commands for ef."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ephemeral_forge import fleet
from ephemeral_forge.config import load_config

app = typer.Typer(
    name="ef",
    help="Ephemeral compute fleet orchestrator — spot instances, "
    "any cloud, tear down when done.",
    no_args_is_help=True,
)
console = Console()


# ── launch ───────────────────────────────────────────────────


@app.command()
def launch(
    count: int = typer.Option(5, "--count", "-n", help="Number of instances"),
    provider: str = typer.Option("aws", "--provider", "-p"),
    region: str = typer.Option(None, "--region", "-r"),
    gpu: bool = typer.Option(False, "--gpu"),
    instance_types: str = typer.Option(
        None,
        "--instance-types",
        "-t",
        help="Comma-separated instance types",
    ),
    tag: str = typer.Option(None, "--tag"),
    config_path: str = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Launch a spot fleet."""
    _setup_logging(verbose)
    config = load_config(Path(config_path) if config_path else None)
    types = instance_types.split(",") if instance_types else None

    try:
        result = fleet.launch(
            provider_name=provider,
            count=count,
            gpu=gpu,
            region=region,
            instance_types=types,
            tag=tag,
            config=config,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    _print_fleet_table(result)

    pconfig = getattr(config, provider, None)
    ssh_user = pconfig.ssh_user if pconfig else "ubuntu"
    key = f"~/.ephemeral-forge/runs/{result.run_id}/private_key.pem"
    console.print(f"\nSSH: [cyan]ssh -i {key} {ssh_user}@<ip>[/cyan]")
    console.print(f"Destroy: [cyan]ef destroy {result.run_id}[/cyan]")


# ── destroy ──────────────────────────────────────────────────


@app.command()
def destroy(
    run_id: str = typer.Argument(None, help="Run ID to destroy"),
    all_fleets: bool = typer.Option(False, "--all", help="Destroy all tracked fleets"),
    config_path: str = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Tear down a fleet and its resources."""
    _setup_logging(verbose)
    config = load_config(Path(config_path) if config_path else None)

    if all_fleets:
        fleet.destroy_all(config)
        console.print("[green]All fleets destroyed[/green]")
    elif run_id:
        fleet.destroy(run_id, config)
        console.print(f"[green]Fleet {run_id} destroyed[/green]")
    else:
        console.print("[red]Specify a run ID or --all[/red]")
        raise typer.Exit(1)


# ── status ───────────────────────────────────────────────────


@app.command()
def status(
    run_id: str = typer.Argument(None, help="Run ID to inspect"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show fleet status."""
    _setup_logging(verbose)
    runs = fleet.list_runs()
    if not runs:
        console.print("No active fleets")
        return

    if run_id:
        runs = [r for r in runs if r == run_id]
        if not runs:
            console.print(f"[red]No fleet {run_id}[/red]")
            raise typer.Exit(1)

    for rid in runs:
        try:
            result = fleet.load_state(rid)
            _print_fleet_table(result)
        except Exception as e:
            console.print(f"[red]{rid}: {e}[/red]")


# ── history ──────────────────────────────────────────────────


@app.command()
def history(
    provider: str = typer.Option(None, "--provider", "-p"),
    last: int = typer.Option(20, "--last", "-n"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show launch history with timing data."""
    _setup_logging(verbose)
    from ephemeral_forge.history import load_history

    records = load_history()
    if provider:
        records = [r for r in records if r.provider == provider]
    records = records[-last:]

    if not records:
        console.print("No launch history")
        return

    table = Table(title="Launch History")
    table.add_column("Run ID")
    table.add_column("Provider")
    table.add_column("Region")
    table.add_column("Count")
    table.add_column("$/hr")
    table.add_column("Launch Time")
    table.add_column("Timestamp")

    for r in records:
        dur = r.launch_duration()
        table.add_row(
            r.run_id,
            r.provider,
            r.region,
            f"{r.count_fulfilled}/{r.count_requested}",
            f"${r.spot_price:.4f}" if r.spot_price else "-",
            f"{dur:.0f}s" if dur > 0 else "-",
            r.timestamp[:19],
        )
    console.print(table)


# ── helpers ──────────────────────────────────────────────────


def _print_fleet_table(result: fleet.FleetResult) -> None:
    title = (
        f"Fleet {result.run_id} — {result.provider} / {result.region} "
        f"({len(result.instances)} instances)"
    )
    table = Table(title=title)
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("Zone")
    table.add_column("Public IP")
    table.add_column("Private IP")

    for inst in result.instances:
        table.add_row(
            inst.id,
            inst.instance_type,
            inst.zone,
            inst.public_ip or "-",
            inst.private_ip,
        )
    console.print(table)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def main() -> None:
    app()
