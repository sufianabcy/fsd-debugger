"""Tests for the step list and how fsd run shows it, using the real logs in tests/logs/."""

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from fsd.checks.spring import CheckResult, diagnose
from fsd.panel import StepPanel, render, render_checks, render_trace
from fsd.session import session_file
from fsd.tracker import RequestTracker, Tracker, load_pipeline

LOGS = Path(__file__).parent / "logs"
FIXTURES = Path(__file__).parent / "fixtures" / "checks"
PY = sys.executable


def plain_console() -> Console:
    return Console(file=io.StringIO(), width=160, color_system=None)


def log_bytes(name: str) -> list[bytes]:
    return (LOGS / f"{name}.log").read_bytes().splitlines(keepends=True)


def drawn(name: str) -> str:
    tracker = Tracker(load_pipeline("spring_startup"))
    for line in log_bytes(name):
        tracker.feed(line.decode())
    tracker.finish()
    console = plain_console()
    console.print(render(tracker))
    return console.file.getvalue()


def rows(text: str) -> list[str]:
    """The step rows of a drawn panel, without the border."""
    return [" ".join(line.strip("│ ").split()) for line in text.splitlines()[1:-1]]


def test_healthy_startup_draws_ten_ticks_with_notes():
    assert rows(drawn("healthy_startup")) == [
        "✓ 1 Build",
        "✓ 2 Start the app DemoAppApplication, Java 21.0.9",
        "✓ 3 Load configuration",
        "✓ 4 Prepare web server port 8080",
        "✓ 5 Connect to database",
        "✓ 6 Set up entities",
        "✓ 7 Create beans",
        "✓ 8 Register endpoints 5 mappings",
        "✓ 9 Open the port port 8080",
        "✓ 10 Ready in 1.576 s",
    ]


def test_missing_service_shows_who_needs_which_bean():
    text = drawn("missing_service_annotation")

    assert "fsd · Startup" in text.splitlines()[0]
    assert rows(text)[5:8] == [
        "✓ 6 Set up entities",
        "✗ 7 Create beans UserController needs a UserService, none found",
        "· 8 Register endpoints",
    ]


def test_each_failure_has_a_short_message():
    assert "✗ 6 Set up entities User is not a JPA entity" in rows(drawn("missing_entity_annotation"))
    assert "✗ 9 Open the port port 8080 is already in use" in rows(drawn("port_in_use"))
    assert (
        "✗ 8 Register endpoints GET /display is mapped twice: "
        "DisplayController#display() and DisplayController#showDisplay()"
    ) in rows(drawn("duplicate_mapping"))
    assert "✗ 1 Build UserService.java:20 cannot find method findEverything()" in rows(drawn("compile_error"))
    assert "✗ 3 Load configuration server.port=abc is not a valid Integer" in rows(drawn("bad_port_property"))
    assert (
        "✗ 5 Connect to database nothing accepted the connection at localhost:9999"
        in rows(drawn("database_refused"))
    )


def feed_panel(name: str, stop_after: str | None = None) -> StepPanel:
    panel = StepPanel(Tracker(load_pipeline("spring_startup")), plain_console())
    for line in log_bytes(name):
        panel.on_line(line)
        if stop_after and stop_after.encode() in line:
            break
    return panel


def test_panel_is_shown_once_startup_is_ready_and_not_again_at_exit():
    panel = feed_panel("healthy_startup", stop_after=" Started ")
    assert panel.console.file.getvalue().count("fsd · Startup") == 1

    panel.on_exit()
    assert panel.console.file.getvalue().count("fsd · Startup") == 1


def test_failed_startup_shows_the_panel_only_when_the_app_exits():
    panel = feed_panel("missing_service_annotation")
    assert panel.console.file.getvalue() == ""

    panel.on_exit()
    assert "✗ 7 Create beans UserController needs a UserService, none found" in rows(panel.console.file.getvalue())


def test_nothing_is_shown_for_output_that_is_not_spring_boot():
    panel = StepPanel(Tracker(load_pipeline("spring_startup")), plain_console())
    panel.on_line(b"hello from the app\n")
    panel.on_exit()

    assert panel.console.file.getvalue() == ""


def replay(name: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run fsd in `cwd` with a fake app that prints a real log."""
    script = f"import sys; sys.stdout.buffer.write(open({str(LOGS / f'{name}.log')!r}, 'rb').read())"
    return subprocess.run([PY, "-m", "fsd", "run", "--", PY, "-c", script], capture_output=True, cwd=cwd)


def test_fsd_run_keeps_the_app_output_unchanged_and_adds_the_panel_on_stderr():
    result = replay("healthy_startup")

    assert result.stdout == (LOGS / "healthy_startup.log").read_bytes()
    assert result.stderr.decode().count("✓") == 10


def test_fsd_run_shows_where_a_failed_startup_stopped():
    result = replay("duplicate_mapping")

    assert result.stdout == (LOGS / "duplicate_mapping.log").read_bytes()
    assert "✗ 8 Register endpoints" in " ".join(result.stderr.decode().split())


# --- checks under the panel ---


def compact(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines()]


def drawn_checks(results: list[CheckResult]) -> list[str]:
    console = plain_console()
    console.print(render_checks(results))
    return compact(console.file.getvalue())


def test_checks_are_drawn_with_their_location_and_the_fix():
    results = [
        CheckResult(True, "interface UserService exists", file=Path("src/UserService.java"), line=3),
        CheckResult(False, "UserServiceImpl has no bean annotation", "add @Service", Path("src/UserServiceImpl.java"), 3),
    ]

    assert drawn_checks(results) == [
        "Checks",
        "✓ interface UserService exists",
        "src/UserService.java:3",
        "✗ UserServiceImpl has no bean annotation",
        "src/UserServiceImpl.java:3",
        "",
        "Fix: add @Service",
    ]


def test_a_message_over_several_lines_keeps_its_caret_aligned():
    result = CheckResult(False, "A.java:3 cannot find method x()\nreturn a.x();\n        ^", "fix it")

    lines = plain_console()
    lines.print(render_checks([result]))
    text = lines.file.getvalue().splitlines()

    code, caret = text[2], text[3]
    assert caret.index("^") == code.index(".x()")


def test_a_note_is_drawn_under_the_fix():
    result = CheckResult(False, "GET /users is mapped", "call /users", note="since Spring Boot 3, /users/ and /users differ")

    assert drawn_checks([result])[-2:] == ["Fix: call /users", "Note: since Spring Boot 3, /users/ and /users differ"]


def test_when_every_check_passes_no_cause_is_reported():
    assert drawn_checks([CheckResult(True, "class UserService exists")])[-1] == "No known cause found: every check passed."


def test_checks_are_printed_under_the_panel_on_a_failure():
    panel = StepPanel(
        Tracker(load_pipeline("spring_startup")),
        plain_console(),
        lambda failure: diagnose(failure, FIXTURES / "bean_interface"),
    )
    for line in log_bytes("missing_service_annotation"):
        panel.on_line(line)
    panel.on_exit()

    panel_text, checks = panel.console.file.getvalue().split("Checks")
    assert "✗  7 Create beans" in panel_text
    assert " ".join(checks.split()) == (
        "✓ interface UserService exists src/main/java/com/demo/service/UserService.java:3 "
        "✗ UserServiceImpl implements UserService but has no bean annotation "
        "src/main/java/com/demo/service/UserServiceImpl.java:3 "
        'Fix: add @Service on the line above "public class UserServiceImpl implements UserService" '
        "(UserServiceImpl.java:3), with import org.springframework.stereotype.Service;"
    )


def test_no_checks_when_startup_passes():
    calls = []
    panel = StepPanel(Tracker(load_pipeline("spring_startup")), plain_console(), calls.append)
    for line in log_bytes("healthy_startup"):
        panel.on_line(line)
    panel.on_exit()

    assert calls == []
    assert "Checks" not in panel.console.file.getvalue()


def test_a_check_that_crashes_does_not_hide_the_step_list():
    def broken(failure):
        raise ValueError("bad source")

    panel = StepPanel(Tracker(load_pipeline("spring_startup")), plain_console(), broken)
    for line in log_bytes("port_in_use"):
        panel.on_line(line)
    panel.on_exit()

    text = panel.console.file.getvalue()
    assert "✗  9 Open the port" in text
    assert "fsd: could not run the checks: ValueError('bad source')" in text


def test_fsd_run_checks_the_project_in_the_current_folder():
    result = replay("missing_service_annotation", cwd=FIXTURES / "bean_interface")
    stderr = " ".join(result.stderr.decode().split())

    assert result.stdout == (LOGS / "missing_service_annotation.log").read_bytes()
    assert "✓ interface UserService exists src/main/java/com/demo/service/UserService.java:3" in stderr
    assert 'Fix: add @Service on the line above "public class UserServiceImpl implements UserService"' in stderr


# --- requests in fsd run ---

DEMO_APP = Path(__file__).parent.parent / "samples" / "demo-app"


def demo_copy(tmp_path: Path) -> Path:
    folder = tmp_path / "demo-app"
    shutil.copytree(DEMO_APP / "src", folder / "src")
    return folder


def request_trace(log: str):
    found = []
    tracker = RequestTracker(load_pipeline("spring_request"), found.append)
    for line in log_bytes(log):
        tracker.feed(line.decode())
    tracker.finish()
    return found[0]


def test_request_panel_for_a_404_has_both_failed_steps():
    console = plain_console()
    console.print(render_trace(request_trace("display_typo_404"), []))
    text = console.file.getvalue()

    assert "fsd · GET /display" in text.splitlines()[0]
    assert rows(text) == [
        '✓ 1 Reach the app GET "/display"',
        "✓ 2 Pass filters",
        "✗ 3 Match a controller nothing is mapped to /display",
        "· 4 Read request data",
        "· 5 Run your code",
        "· 6 Run SQL",
        "✗ 7 Send the response 404 NOT_FOUND",
    ]


@pytest.mark.parametrize(
    ("log", "row"),
    [
        ("trailing_slash_404", "✗ 3 Match a controller nothing is mapped to /users/"),
        ("wrong_method_405", "✗ 3 Match a controller this path does not accept DELETE"),
        (
            "post_malformed_json",
            "✗ 4 Read request data the body is not valid JSON: Unexpected end-of-input: expected close marker for Object",
        ),
        ("post_wrong_field_type", '✗ 4 Read request data the body has "abc", which is not a valid Long'),
        ("path_variable_type_400", "✗ 4 Read request data id=abc is not a valid Long"),
        ("query_param_type_400", "✗ 4 Read request data limit=ten is not a valid int"),
        ("missing_request_param_400", "✗ 4 Read request data ?name= is required, and the request has none"),
        ("missing_column_sql", "✗ 6 Run SQL the database has no column EMAIL"),
        ("missing_table_sql", "✗ 6 Run SQL the database has no table POSTS"),
        ("duplicate_key_sql", "✗ 6 Run SQL 'Ada' is already in USERS.NAME, which must be unique"),
        ("lazy_initialization", "✗ 6 Run SQL User.posts was read after the database session closed"),
        ("json_recursion", "✗ 7 Send the response the JSON never ends: Jackson stopped 501 levels deep"),
    ],
)
def test_each_request_failure_has_a_short_message(log, row):
    found = []
    tracker = RequestTracker(load_pipeline("spring_request"), found.append)
    for line in log_bytes(log):
        tracker.feed(line.decode())
    console = plain_console()
    console.print(render_trace(next(t for t in found if t.run.failure), []))

    assert row in rows(console.file.getvalue())


def test_fsd_run_prints_one_line_for_a_successful_request(tmp_path):
    result = replay("healthy_get_users", cwd=demo_copy(tmp_path))
    stderr = result.stderr.decode()

    assert result.stdout == (LOGS / "healthy_get_users.log").read_bytes()
    assert "✓ GET /users 200" in stderr.splitlines()
    assert "fsd · GET" not in stderr


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        ("display_typo_404", ["fsd · GET /display", "✗ 3 Match a controller nothing is mapped to /display"]),
        ("post_missing_body", ["fsd · POST /users", "✗ 4 Read request data UserController.create() needs a request body"]),
        (
            "service_exception_500",
            [
                "fsd · GET /users",
                "✗ 5 Run your code IllegalStateException: findAll is broken on purpose",
                "How deep it got",
                "→ controller UserController.list() UserController.java:26",
                "✗ service UserService.findAll() UserService.java:20",
            ],
        ),
    ],
)
def test_fsd_run_prints_the_panel_for_a_failed_request(tmp_path, log, expected):
    result = replay(log, cwd=demo_copy(tmp_path))
    stderr = " ".join(result.stderr.decode().split())

    assert result.stdout == (LOGS / f"{log}.log").read_bytes()
    for text in expected:
        assert text in stderr


def test_fsd_run_prints_the_checks_under_a_failed_request(tmp_path):
    result = replay("wrong_method_405", cwd=demo_copy(tmp_path))
    stderr = " ".join(result.stderr.decode().split())

    assert "✗ 3 Match a controller this path does not accept DELETE" in stderr
    assert "Checks ✗ /users/1 accepts GET, not DELETE: GET /users/{id} UserController#get() UserController.java:36" in stderr
    assert 'Fix: send GET /users/1, or add a method with @DeleteMapping("/{id}") to UserController for DELETE' in stderr


def test_fsd_run_shows_the_panel_for_a_200_that_failed_while_writing_json(tmp_path):
    result = replay("json_recursion", cwd=demo_copy(tmp_path))
    stderr = " ".join(result.stderr.decode().split())

    assert "✓ POST /users 200" in stderr
    assert "✓ GET /users 200" not in stderr
    assert "fsd · GET /users" in stderr
    assert "✗ 7 Send the response the JSON never ends: Jackson stopped 501 levels deep" in stderr


def test_fsd_run_shows_the_browser_icon_404_as_one_dim_line(tmp_path):
    result = replay("browser_favicon_404", cwd=demo_copy(tmp_path))
    stderr = result.stderr.decode()

    assert result.stdout == (LOGS / "browser_favicon_404.log").read_bytes()
    assert stderr.splitlines()[-2:] == [
        "✓ GET /display 200",
        "· GET /favicon.ico 404  the browser asking for a tab icon; safe to ignore",
    ]
    assert "fsd · GET /favicon.ico" not in stderr
    assert "Checks" not in stderr


def test_fsd_run_adds_each_request_to_the_session_file_and_never_writes_into_the_project(tmp_path):
    project = demo_copy(tmp_path)
    before = sorted(project.rglob("*"))

    replay("display_typo_404", cwd=project)

    assert sorted(project.rglob("*")) == before
    records = [json.loads(line) for line in session_file(project).read_text().splitlines()]
    assert [(r["method"], r["path"], r["status"], r["cause"]) for r in records] == [("GET", "/display", 404, "no_handler")]
