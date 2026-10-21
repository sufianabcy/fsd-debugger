"""fsd try: send one request to the app, then show the trace fsd run logged for it.

The request is sent with the standard library only. The trace comes from the
session file that fsd run writes, so fsd try works while fsd run is running
the app in the same project folder.
"""

import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.text import Text

from fsd.checks.spring import diagnose_request
from fsd.panel import print_checks, render, render_trace, trace_code_path
from fsd.session import session_file, trace_from_record, wait_for_record
from fsd.tracker import Failure, Pipeline, PipelineRun, load_pipeline

WAIT_SECONDS = 5.0
REQUEST_TIMEOUT = 30.0
START_HINT = "fsd run -- ./mvnw spring-boot:run"
# No proxy: the request goes straight to the app on this machine.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(method: str, path: str, port: int, body: str | None) -> int:
    """Send the request and return the response status; raise ConnectionRefusedError if nothing listens."""
    data = body.encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(f"http://localhost:{port}{path}", data=data, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            raise error.reason from None
        raise


def not_reached(pipeline: Pipeline, port: int) -> PipelineRun:
    """A run whose first step failed: nothing answered on `port`."""
    run = PipelineRun(pipeline)
    step, rule = pipeline.rule("not_listening")
    run.record(Failure(step, rule, {"port": str(port)}, event=""))
    return run


def try_request(method: str, path: str, body: str | None, port: int, console: Console, project: Path) -> int:
    """Send `method` `path` to the app on `port`, print the steps it went through, and return the exit code."""
    pipeline = load_pipeline("spring_request")
    method = method.upper()
    path = path if path.startswith("/") else f"/{path}"
    request = f"{method} {path}"
    file = session_file(project)
    offset = file.stat().st_size if file.exists() else 0
    since = time.time()
    try:
        status = send(method, path, port, body)
    except ConnectionRefusedError:
        console.print(render(not_reached(pipeline, port), title=request))
        return 1
    except OSError as error:
        console.print(Text(f"fsd: could not send {request} to port {port}: {getattr(error, 'reason', error)}"))
        return 1
    if not file.exists():
        console.print(Text(
            f"{request} answered {status}, but there is no fsd run session for this folder, "
            f"so fsd cannot show its steps. Start the app here with: {START_HINT}"
        ))  # fmt: skip
        return 1
    record = wait_for_record(file, method, urlsplit(path).path, offset, since, WAIT_SECONDS)
    trace = trace_from_record(pipeline, record) if record else None
    if trace is None:
        console.print(Text(
            f"{request} answered {status}, but fsd run logged no trace of it within {WAIT_SECONDS:g}s. "
            f"Is the app on port {port} the one fsd run started in this folder?"
        ))  # fmt: skip
        return 1
    console.print(render_trace(trace, trace_code_path(trace, project)))
    print_checks(console, lambda: diagnose_request(trace, project))
    return 0 if trace.succeeded else 1
