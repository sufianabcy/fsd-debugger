"""Draw a pipeline's step list with ✓ ✗ ·, the checks run on a failure, and each request's trace."""

import re
from collections.abc import Callable
from pathlib import Path

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fsd.checks.spring import CheckResult, diagnose_request
from fsd.scanner.spring import scan, simple_type_name
from fsd.session import append_trace
from fsd.tracker import CodeFrame, Failure, Pipeline, PipelineRun, RequestTracker, Status, Step, Trace, Tracker, code_path

SYMBOLS = {
    Status.PASSED: ("✓", "green"),
    Status.FAILED: ("✗", "bold red"),
    Status.WAITING: ("·", "dim"),
}
# Browsers ask every site for a tab icon on their own; its 404 is not a bug in the app.
BROWSER_ICON = "/favicon.ico"

# The package in front of a class name: com.demo.service.UserService -> UserService.
JAVA_PACKAGE = re.compile(r"\b(?:[a-z_][\w$]*\.)+(?=[A-Z])")


def short_names(details: dict[str, str]) -> dict[str, str]:
    return {name: JAVA_PACKAGE.sub("", value) for name, value in details.items()}


def fill(template: str, details: dict[str, str]) -> str:
    """Fill `template` from `details`, or return "" if a detail it needs is missing."""
    try:
        return template.format(**short_names(details))
    except (KeyError, IndexError, ValueError):
        return ""


def failure_message(failure: Failure) -> str:
    return fill(failure.rule.message, failure.details) or failure.rule.title


def step_note(run: PipelineRun, step: Step) -> Text:
    status = run.status[step.id]
    if status is Status.FAILED:
        return Text(failure_message(run.failures[step.id]), style="red")
    if status is Status.PASSED and step.note:
        return Text(fill(step.note, run.details[step.id]), style="dim")
    return Text("")


def render(run: PipelineRun, title: str | None = None) -> Panel:
    rows = Table.grid(padding=(0, 1))
    rows.add_column()
    rows.add_column(justify="right")
    rows.add_column()
    rows.add_column()
    for number, step in enumerate(run.pipeline.steps, start=1):
        symbol, style = SYMBOLS[run.status[step.id]]
        name_style = style if run.status[step.id] is Status.FAILED else ""
        rows.add_row(Text(symbol, style), str(number), Text(step.name, name_style), step_note(run, step))
    return Panel(rows, title=f"fsd · {title or run.pipeline.title}", title_align="left", expand=False)


def frame_name(frame: CodeFrame) -> str:
    """UserService.findAll() for com.demo.service.UserService$$SpringCGLIB$$0.findAll."""
    return f"{simple_type_name(frame.frame.class_name.split('$$')[0])}.{frame.frame.method}()"


def render_code_path(path: list[CodeFrame]) -> Padding:
    """The app's own methods the request went through, outermost first; the last one is where it failed."""
    rows = Table.grid(padding=(0, 2))
    for index, frame in enumerate(path):
        failed = index == len(path) - 1
        symbol = Text("✗", "bold red") if failed else Text("→", "dim")
        where = f"{frame.frame.file}:{frame.frame.line}" if frame.frame.line else frame.frame.file
        rows.add_row(symbol, Text(frame.role, "dim"), frame_name(frame), Text(where, "dim"))
    return Padding(Group(Text("How deep it got", style="bold"), Padding(rows, (0, 0, 0, 2))), (0, 0, 0, 2))


def render_trace(trace: Trace, path: list[CodeFrame]) -> Group:
    parts = [render(trace.run, title=f"{trace.method} {trace.path}")]
    if path:
        parts.append(render_code_path(path))
    return Group(*parts)


def trace_code_path(trace: Trace, root: Path) -> list[CodeFrame]:
    """The trace's stack frames in the project at `root`, labeled; the project is only read if there are any."""
    return code_path(trace.frames, scan(root).role) if trace.frames else []


def is_browser_icon_404(trace: Trace) -> bool:
    return trace.path == BROWSER_ICON and trace.status == 404


def show_request(console: Console, trace: Trace, root: Path) -> None:
    """One line for a request that succeeded or the browser's icon, the panel and its checks for one that failed."""
    request = f"{trace.method} {trace.path} {trace.status}"
    if trace.succeeded:
        console.print(Text.assemble(("✓ ", "green"), request))
    elif is_browser_icon_404(trace):
        console.print(Text(f"· {request}  the browser asking for a tab icon; safe to ignore", style="dim"))
    else:
        console.print(render_trace(trace, trace_code_path(trace, root)))
        print_checks(console, lambda: diagnose_request(trace, root))


def check_symbol(result: CheckResult) -> Text:
    symbol, style = SYMBOLS[Status.PASSED if result.passed else Status.FAILED]
    return Text(symbol, style)


def check_text(result: CheckResult) -> Text:
    """The message, with the file and line on a line of their own so a narrow terminal never cuts them."""
    text = Text(result.message)
    if result.location:
        text.append("\n" + result.location, style="dim")
    return text


def render_checks(results: list[CheckResult]) -> Padding:
    """The checks that ran, then the fix from the first one that failed."""
    rows = Table.grid(padding=(0, 1))
    for result in results:
        rows.add_row(check_symbol(result), check_text(result))
    parts = [Text("Checks", style="bold"), Padding(rows, (0, 0, 0, 2))]
    failed = next((r for r in results if not r.passed), None)
    if failed is None:
        parts += [Text(), Text("No known cause found: every check passed.", style="yellow")]
    elif failed.fix:
        parts += [Text(), Text.assemble(("Fix: ", "bold green"), failed.fix)]
    if failed is not None and failed.note:
        parts.append(Text.assemble(("Note: ", "bold"), failed.note))
    return Padding(Group(*parts), (0, 0, 0, 2))


def print_checks(console: Console, run_checks: Callable[[], list[CheckResult]]) -> None:
    """Run the checks and print them; a bug in a check must not hide the step list printed above."""
    try:
        results = run_checks()
    except Exception as error:
        console.print(Text(f"fsd: could not run the checks: {error!r}", style="yellow"))
        return
    if results:
        console.print(render_checks(results))


class StepPanel:
    """Feed it the app's output lines; it prints the step list once the pipeline ends.

    Printed as soon as every step has passed, otherwise when the app exits, so
    it always comes after the app's own lines and never between them. On a
    failure, `diagnose` runs its checklist and the checks are printed below.
    """

    def __init__(
        self,
        tracker: Tracker,
        console: Console,
        diagnose: Callable[[Failure], list[CheckResult]] | None = None,
    ):
        self.tracker = tracker
        self.console = console
        self.diagnose = diagnose
        self.shown = False

    def on_line(self, line: bytes) -> None:
        self.tracker.feed(line.decode("utf-8", errors="replace"))
        if self.tracker.all_passed and not self.shown:
            self.show()

    def on_exit(self) -> None:
        self.tracker.finish()
        if self.tracker.has_progress and not self.shown:
            self.show()

    def show(self) -> None:
        self.console.print(render(self.tracker))
        self.shown = True
        failure = self.tracker.failure
        if failure is not None and self.diagnose is not None:
            print_checks(self.console, lambda: self.diagnose(failure))


class RequestPanel:
    """Feed it the app's output lines; it shows each request as it ends and adds it to the session file."""

    def __init__(self, pipeline: Pipeline, console: Console, project: Path, session: Path | None = None):
        self.tracker = RequestTracker(pipeline, self.finished)
        self.console = console
        self.project = project
        self.session = session

    def on_line(self, line: bytes) -> None:
        self.tracker.feed(line.decode("utf-8", errors="replace"))

    def on_exit(self) -> None:
        self.tracker.finish()

    def finished(self, trace: Trace) -> None:
        if self.session is not None:
            append_trace(self.session, trace)
        show_request(self.console, trace, self.project)
