"""Command line entry point: fsd run / try / doctor."""

import signal
import sys

import typer

from fsd import runner

app = typer.Typer(
    help="Step-by-step flow tracer for Spring Boot.",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_COMMAND_NOT_FOUND = 127


@app.callback()
def main() -> None:
    """Step-by-step flow tracer for Spring Boot."""


def raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def stop_app_when_terminated() -> None:
    """Treat a terminal closing or an IDE stop button like Ctrl+C, so the app stops too."""
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), raise_keyboard_interrupt)


@app.command(context_settings={"ignore_unknown_options": True})
def run(
    command: list[str] = typer.Argument(
        ..., help="The command that starts your app, after --. Example: fsd run -- ./mvnw spring-boot:run"
    ),
) -> None:
    """Start your app and show its output unchanged."""
    stop_app_when_terminated()
    try:
        code = runner.run(command, sys.stdout.buffer)
    except FileNotFoundError:
        typer.echo(f"fsd: command not found: {command[0]}", err=True)
        code = EXIT_COMMAND_NOT_FOUND
    raise typer.Exit(code)
