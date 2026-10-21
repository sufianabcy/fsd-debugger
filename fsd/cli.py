"""Command line entry point: fsd run / try / doctor."""

import signal
import sys
from pathlib import Path

import typer
from rich.console import Console

from fsd import client, runner
from fsd.checks.spring import diagnose
from fsd.panel import RequestPanel, StepPanel
from fsd.session import start_session
from fsd.tracker import Tracker, load_pipeline

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
    """Start your app, show its output unchanged, then the startup steps and each request."""
    stop_app_when_terminated()
    project = Path.cwd()
    console = Console(stderr=True)
    startup = StepPanel(Tracker(load_pipeline("spring_startup")), console, diagnose)
    requests = RequestPanel(load_pipeline("spring_request"), console, project, start_session(project))

    def on_line(line: bytes) -> None:
        startup.on_line(line)
        requests.on_line(line)

    try:
        code = runner.run(command, sys.stdout.buffer, on_line=on_line)
    except FileNotFoundError:
        typer.echo(f"fsd: command not found: {command[0]}", err=True)
        code = EXIT_COMMAND_NOT_FOUND
    startup.on_exit()
    requests.on_exit()
    raise typer.Exit(code)


@app.command("try")
def try_request(
    method: str = typer.Argument(..., help="The HTTP method: GET, POST, PUT, PATCH or DELETE."),
    path: str = typer.Argument(..., help="The path to call, like /users."),
    body: str | None = typer.Option(None, "--body", help="A JSON body to send, like '{\"name\": \"Ada\"}'."),
    port: int = typer.Option(8080, "--port", help="The port your app listens on."),
) -> None:
    """Send one request to your app (started with fsd run) and show the steps it went through."""
    raise typer.Exit(client.try_request(method, path, body, port, Console(), Path.cwd()))
