"""Tests for fsd try, against a tiny fake HTTP server and a session file written as fsd run would."""

import io
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from fsd import client
from fsd.cli import app
from fsd.session import append_trace, session_file, start_session
from fsd.tracker import RequestTracker, load_pipeline

LOGS = Path(__file__).parent / "logs"
DEMO_APP = Path(__file__).parent.parent / "samples" / "demo-app"
REQUEST = load_pipeline("spring_request")


def logged_trace(log: str):
    """The last request in `log`."""
    found = []
    tracker = RequestTracker(REQUEST, found.append)
    for line in (LOGS / f"{log}.log").open(encoding="utf-8"):
        tracker.feed(line)
    tracker.finish()
    return found[-1]


class FakeApp:
    """Answers every request with `status`, and logs `log`'s trace to the session file shortly after, like fsd run."""

    def __init__(self, project: Path, status: int = 200, log: str | None = None, delay: float = 0.2):
        self.received: list[tuple[str, str, dict, bytes]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                fake.received.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
                if log:
                    threading.Timer(delay, fake.log_trace).start()
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

            def log_message(self, *args):
                pass

        self.project, self.log = project, log
        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def log_trace(self):
        trace = logged_trace(self.log)
        trace.finished_at = time.time()
        append_trace(session_file(self.project), trace)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def project(tmp_path):
    """A copy of the demo app's source, with an fsd run session."""
    folder = tmp_path / "demo-app"
    shutil.copytree(DEMO_APP / "src", folder / "src")
    start_session(folder)
    return folder


@pytest.fixture
def fake_app(project):
    apps = []

    def make(**options) -> FakeApp:
        apps.append(FakeApp(project, **options))
        return apps[-1]

    yield make
    for running in apps:
        running.stop()


def try_it(project: Path, method: str, path: str, port: int, body: str | None = None) -> tuple[int, list[str]]:
    console = Console(file=io.StringIO(), width=120, color_system=None)
    code = client.try_request(method, path, body, port, console, project)
    return code, [" ".join(line.strip("│ ").split()) for line in console.file.getvalue().splitlines()]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


def test_successful_request_shows_every_step(project, fake_app):
    running = fake_app(status=200, log="healthy_get_users")

    code, lines = try_it(project, "GET", "/users", running.port)

    assert code == 0
    assert "fsd · GET /users" in lines[0]
    assert lines[1:8] == [
        '✓ 1 Reach the app GET "/users"',
        "✓ 2 Pass filters",
        "✓ 3 Match a controller UserController#list()",
        "✓ 4 Read request data",
        "✓ 5 Run your code",
        "✓ 6 Run SQL select u1_0.id,u1_0.name from users u1_0",
        "✓ 7 Send the response 200 OK",
    ]
    assert [r[:2] for r in running.received] == [("GET", "/users")]


def test_failed_request_shows_how_deep_it_got(project, fake_app):
    running = fake_app(status=500, log="service_exception_500")

    code, lines = try_it(project, "GET", "/users", running.port)

    assert code == 1
    assert "✗ 5 Run your code IllegalStateException: findAll is broken on purpose" in lines
    assert "✗ 7 Send the response 500" in lines
    how_deep = lines.index("How deep it got")
    assert lines[how_deep + 1 :] == [
        "→ controller UserController.list() UserController.java:26",
        "✗ service UserService.findAll() UserService.java:20",
    ]


def test_failed_request_shows_the_checks_and_the_fix(project, fake_app):
    running = fake_app(status=400, log="post_missing_body")

    code, lines = try_it(project, "POST", "/users", running.port)

    assert code == 1
    checks = lines.index("Checks")
    assert lines[checks + 1 : checks + 3] == [
        "✗ UserController.create() reads the body into @RequestBody User user, and the request had none",
        "src/main/java/com/demo/controller/UserController.java:32",
    ]
    assert any(line.startswith("Fix: send a JSON body with Content-Type: application/json") for line in lines)


def test_a_request_with_a_query_finds_its_trace(project, fake_app):
    # Spring logs the path with its query: GET "/users/search?name=Ada&limit=ten".
    running = fake_app(status=400, log="query_param_type_400")

    code, lines = try_it(project, "GET", "/users/search?name=Ada&limit=ten", running.port)

    assert code == 1
    assert "fsd · GET /users/search?name=Ada&limit=ten" in lines[0]
    assert any(line.startswith("Fix: call /users/search?name=Ada&limit=10") for line in lines)


def test_a_200_that_failed_while_writing_json_is_a_failure(project, fake_app):
    post = project / "src/main/java/com/demo/model/Post.java"
    post.write_text(post.read_text().replace("\t@JsonIgnore\n", ""))  # as when json_recursion.log was captured
    running = fake_app(status=200, log="json_recursion")

    code, lines = try_it(project, "GET", "/users", running.port)

    assert code == 1
    assert "✗ 7 Send the response the JSON never ends: Jackson stopped 501 levels deep" in lines
    assert any(line.startswith("Fix: break the loop on one side") for line in lines)


def test_connection_refused_fails_reach_the_app(project):
    port = free_port()

    code, lines = try_it(project, "GET", "/users", port)

    assert code == 1
    assert f"✗ 1 Reach the app nothing is listening on port {port}; start it with fsd run" in lines
    assert "· 7 Send the response" in lines


def test_no_session_file_says_so(project, fake_app):
    session_file(project).unlink()
    running = fake_app(status=200)

    code, lines = try_it(project, "GET", "/users", running.port)

    assert code == 1
    assert "no fsd run session for this folder" in " ".join(lines)
    assert len(running.received) == 1  # the request was still sent


def test_no_trace_in_time_says_so(project, fake_app, monkeypatch):
    monkeypatch.setattr(client, "WAIT_SECONDS", 0.3)
    running = fake_app(status=200)  # answers, but nothing logs a trace

    code, lines = try_it(project, "GET", "/users", running.port)

    assert code == 1
    assert "fsd run logged no trace of it within 0.3s" in " ".join(lines)


def test_a_trace_from_before_the_request_is_not_used(project, fake_app, monkeypatch):
    monkeypatch.setattr(client, "WAIT_SECONDS", 0.3)
    old = logged_trace("healthy_get_users")
    old.finished_at = time.time()
    append_trace(session_file(project), old)
    running = fake_app(status=200)

    code, lines = try_it(project, "GET", "/users", running.port)

    assert code == 1
    assert "logged no trace" in " ".join(lines)


def test_only_a_trace_for_the_same_method_and_path_is_used(project, fake_app, monkeypatch):
    monkeypatch.setattr(client, "WAIT_SECONDS", 0.5)
    running = fake_app(status=200, log="display_typo_404")  # logs GET /display, not /users

    code, lines = try_it(project, "GET", "/users", running.port)

    assert "logged no trace" in " ".join(lines)


def test_method_and_path_are_tidied_and_the_query_is_ignored_for_matching(project, fake_app):
    running = fake_app(status=200, log="healthy_get_users")

    code, lines = try_it(project, "get", "users?page=1", running.port)

    assert code == 0
    assert running.received[0][:2] == ("GET", "/users?page=1")


def test_cli_sends_a_json_body(project, fake_app, monkeypatch):
    running = fake_app(status=400, log="post_missing_body")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(app, ["try", "POST", "/users", "--body", '{"name": "Ada"}', "--port", str(running.port)])

    assert result.exit_code == 1
    method, path, headers, body = running.received[0]
    assert (method, path, body) == ("POST", "/users", b'{"name": "Ada"}')
    assert headers["Content-Type"] == "application/json"
    assert "Read request data" in result.output
