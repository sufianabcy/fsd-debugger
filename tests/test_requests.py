"""Tests for the request pipeline and the request tracker, using the real logs in tests/logs/."""

import re
from pathlib import Path

import pytest

from fsd.scanner.spring import scan
from fsd.tracker import PIPELINES, RequestTracker, Status, code_path, load_pipeline, thread_of, trace_from_lines

LOGS = Path(__file__).parent / "logs"
DEMO_APP = Path(__file__).parent.parent / "samples" / "demo-app"
REQUEST = load_pipeline("spring_request")
REQUEST_LOGS = {
    "healthy_get_users", "service_exception_500", "display_typo_404", "post_missing_body", "trailing_slash_404",
    "singular_path_404", "wrong_method_405", "post_malformed_json", "post_wrong_field_type",
    "path_variable_type_400", "query_param_type_400", "missing_request_param_400", "browser_favicon_404",
    "missing_column_sql", "missing_table_sql", "duplicate_key_sql", "lazy_initialization", "json_recursion",
}  # fmt: skip
STARTUP_LOGS = sorted(path.stem for path in LOGS.glob("*.log") if path.stem not in REQUEST_LOGS)


def log_lines(name: str) -> list[str]:
    return (LOGS / f"{name}.log").read_text(encoding="utf-8").splitlines(keepends=True)


def traces(lines: list[str]):
    found = []
    tracker = RequestTracker(REQUEST, found.append)
    for line in lines:
        tracker.feed(line)
    tracker.finish()
    return found


def one_trace(name: str):
    (trace,) = traces(log_lines(name))
    return trace


def statuses(trace) -> str:
    symbol = {Status.PASSED: "✓", Status.FAILED: "✗", Status.WAITING: "·"}
    return "".join(symbol[trace.run.status[step.id]] for step in REQUEST.steps)


def test_pipeline_has_the_seven_request_steps_in_order():
    assert [step.name for step in REQUEST.steps] == [
        "Reach the app",
        "Pass filters",
        "Match a controller",
        "Read request data",
        "Run your code",
        "Run SQL",
        "Send the response",
    ]


def test_patterns_never_hard_code_the_app():
    text = (PIPELINES / "spring_request.yaml").read_text(encoding="utf-8")

    for forbidden in ("8080", "DemoApp", "com.demo", "UserController", "/users", "/display"):
        assert forbidden not in text


@pytest.mark.parametrize(
    ("log", "method_and_path", "status", "steps"),
    [
        ("healthy_get_users", ("GET", "/users"), 200, "✓✓✓✓✓✓✓"),
        ("display_typo_404", ("GET", "/display"), 404, "✓✓✗···✗"),
        ("post_missing_body", ("POST", "/users"), 400, "✓✓✓✗··✗"),
        ("service_exception_500", ("GET", "/users"), 500, "✓✓✓✓✗·✗"),
        ("trailing_slash_404", ("GET", "/users/"), 404, "✓✓✗···✗"),
        ("singular_path_404", ("GET", "/user/1"), 404, "✓✓✗···✗"),
        ("wrong_method_405", ("DELETE", "/users/1"), 405, "✓✓✗···✗"),
        ("post_malformed_json", ("POST", "/users"), 400, "✓✓✓✗··✗"),
        ("post_wrong_field_type", ("POST", "/users"), 400, "✓✓✓✗··✗"),
        ("path_variable_type_400", ("GET", "/users/abc"), 400, "✓✓✓✗··✗"),
        # Spring logs the query with the path.
        ("query_param_type_400", ("GET", "/users/search?name=Ada&limit=ten"), 400, "✓✓✓✗··✗"),
        ("missing_request_param_400", ("GET", "/users/search"), 400, "✓✓✓✗··✗"),
    ],
)
def test_each_request_log_gives_one_trace(log, method_and_path, status, steps):
    trace = one_trace(log)

    assert (trace.method, trace.path) == method_and_path
    assert trace.thread == "nio-8080-exec-1"
    assert trace.status == status
    assert statuses(trace) == steps


@pytest.mark.parametrize("log", STARTUP_LOGS)
def test_startup_logs_have_no_requests(log):
    assert traces(log_lines(log)) == []


def test_healthy_request_keeps_the_handler_and_the_sql():
    details = one_trace("healthy_get_users").run.details

    assert details["match_controller"] == {"controller": "com.demo.controller.UserController", "handler": "list"}
    assert details["run_sql"] == {"sql": "select u1_0.id,u1_0.name from users u1_0"}
    assert details["send_response"] == {"response": "200 OK"}


def test_no_mapping_fails_match_a_controller_and_the_status_fails_the_response():
    run = one_trace("display_typo_404").run

    assert run.failure.step.name == "Match a controller"
    assert run.failure.details == {"resource": "display", "request_path": "/display"}
    assert run.failures["send_response"].details == {"response": "404 NOT_FOUND", "status": "404"}


def test_missing_body_fails_read_request_data():
    run = one_trace("post_missing_body").run

    assert run.failure.step.name == "Read request data"
    assert run.failure.details == {
        "controller": "com.demo.controller.UserController",
        "handler": "create",
        "parameter_types": "com.demo.model.User",
    }


@pytest.mark.parametrize(
    ("log", "rule", "details"),
    [
        ("trailing_slash_404", "no_handler", {"resource": "users", "request_path": "/users/"}),
        ("wrong_method_405", "method_not_allowed", {"http_method": "DELETE"}),
        (
            "post_malformed_json",
            "malformed_json",
            {"json_error": "Unexpected end-of-input: expected close marker for Object"},
        ),
        ("post_wrong_field_type", "json_wrong_type", {"target_type": "java.lang.Long", "json_value": '"abc"'}),
        (
            "path_variable_type_400",
            "argument_type_mismatch",
            {"parameter": "id", "source_type": "java.lang.String", "target_type": "java.lang.Long", "value": "abc"},
        ),
        ("missing_request_param_400", "missing_request_param", {"parameter": "name", "parameter_type": "String"}),
    ],
)
def test_each_4xx_log_names_its_cause(log, rule, details):
    run = one_trace(log).run

    assert (run.failure.rule.id, run.failure.details) == (rule, details)


def test_a_400_keeps_the_handler_it_was_mapped_to():
    details = one_trace("path_variable_type_400").run.details

    assert details["match_controller"] == {"controller": "com.demo.controller.UserController", "handler": "get"}


def test_a_browser_loads_the_page_then_asks_for_its_icon():
    found = traces(log_lines("browser_favicon_404"))

    assert [(t.thread, t.method, t.path, t.status, statuses(t)) for t in found] == [
        ("nio-8080-exec-1", "GET", "/display", 200, "✓✓✓✓✓✓✓"),
        ("nio-8080-exec-2", "GET", "/favicon.ico", 404, "✓✓✗···✗"),
    ]


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        ("missing_column_sql", [("GET", "/users", 500, "✓✓✓✓✓✗✗", "missing_column")]),
        ("missing_table_sql", [("GET", "/posts", 500, "✓✓✓✓✓✗✗", "missing_table")]),
        (
            "duplicate_key_sql",
            [("POST", "/users", 200, "✓✓✓✓✓✓✓", None), ("POST", "/users", 500, "✓✓✓✓✓✗✗", "duplicate_key")],
        ),
        (
            "lazy_initialization",
            [("POST", "/users", 200, "✓✓✓✓✓✓✓", None), ("GET", "/users", 500, "✓✓✓✓✓✗✗", "lazy_initialization")],
        ),
        (
            # The response was committed before Jackson gave up, so Spring still logs "Completed 200 OK".
            "json_recursion",
            [
                ("POST", "/users", 200, "✓✓✓✓✓✓✓", None),
                ("POST", "/users/1/posts", 200, "✓✓✓✓✓✓✗", "json_recursion"),
                ("GET", "/users", 200, "✓✓✓✓✓✓✗", "json_recursion"),
            ],
        ),
    ],
)
def test_sql_and_response_logs(log, expected):
    found = traces(log_lines(log))

    assert [(t.method, t.path, t.status, statuses(t), t.run.failure and t.run.failure.rule.id) for t in found] == expected


@pytest.mark.parametrize(
    ("log", "details"),
    [
        ("missing_column_sql", {"alias": "U1_0", "column": "EMAIL", "sql": "select u1_0.id,u1_0.email,u1_0.name from users u1_0"}),
        ("missing_table_sql", {"table": "POSTS", "sql": "select p1_0.id,p1_0.title,p1_0.user_id from posts p1_0"}),
        (
            "duplicate_key_sql",
            {
                "constraint": "PUBLIC.CONSTRAINT_4D INDEX PUBLIC.CONSTRAINT_INDEX_4",
                "table": "USERS",
                "columns": "NAME NULLS FIRST",
                "column": "NAME",
                "value": "'Ada'",
                "sql": "insert into users (name,id) values (?,?)",
            },
        ),
        ("lazy_initialization", {"writing_json": "Could not write JSON: ", "role": "com.demo.model.User.posts", "key": "1"}),
        ("json_recursion", {"depth": "501", "max_depth": "500"}),
    ],
)
def test_each_sql_and_response_log_names_its_cause(log, details):
    assert traces(log_lines(log))[-1].run.failure.details == details


def test_an_sql_error_beats_the_exception_that_carries_it_to_your_code():
    trace = one_trace("missing_column_sql")

    assert any("Failed to complete request: org.springframework.dao." in line for line in trace.lines)
    assert trace.run.failure.rule.id == "missing_column"
    assert trace.run.status["run_code"] is Status.PASSED


def test_a_generic_exception_gives_way_to_an_sql_error_found_after_it():
    # Drop Hibernate's own error lines and the database's text from "Failed to complete request":
    # then that line is only a generic exception, and the stack trace after it names the column.
    lines = one_trace("missing_column_sql").lines
    first_error = next(i for i, line in enumerate(lines) if "HHH000247" in line)
    failed = next(i for i, line in enumerate(lines) if "Failed to complete request" in line)
    generic_first = lines[:first_error] + [lines[failed].split(" [Column")[0]] + lines[failed + 2 :]

    assert trace_from_lines(REQUEST, generic_first[: first_error + 1]).run.failure.rule.id == "exception"
    trace = trace_from_lines(REQUEST, generic_first)
    assert (trace.run.failure.rule.id, statuses(trace)) == ("missing_column", "✓✓✓✓✓✗✗")


def test_older_wording_of_lazy_loading_and_recursion():
    # Hibernate 6 and Jackson 2, as in Spring Boot 3.
    hibernate_6 = "failed to lazily initialize a collection of role: com.demo.model.User.posts: could not initialize proxy - no Session"
    lazy = [
        line.replace("Cannot lazily initialize collection of role 'com.demo.model.User.posts' with key '1' (no session)", hibernate_6)
        for line in traces(log_lines("lazy_initialization"))[-1].lines
    ]
    jackson_2 = [
        re.sub(r"Document nesting depth \(501\) .*?getMaxNestingDepth\(\)`\)", "Infinite recursion (StackOverflowError)", line)
        for line in traces(log_lines("json_recursion"))[-1].lines
    ]

    assert trace_from_lines(REQUEST, lazy).run.failure.details["role"] == "com.demo.model.User.posts"
    assert any("Infinite recursion (StackOverflowError)" in line for line in jackson_2)
    assert trace_from_lines(REQUEST, jackson_2).run.failure.rule.id == "json_recursion"


def test_exception_fails_run_your_code_and_the_error_page_is_not_a_step():
    trace = one_trace("service_exception_500")

    assert trace.run.failure.step.name == "Run your code"
    assert trace.run.failure.details == {
        "error": "java.lang.IllegalStateException: findAll is broken on purpose",
        "exception": "java.lang.IllegalStateException",
        "exception_message": "findAll is broken on purpose",
    }
    # The error page's own "Mapped to BasicErrorController#error" must not count.
    assert trace.run.details["match_controller"]["controller"] == "com.demo.controller.UserController"
    assert trace.run.failures["send_response"].details["status"] == "500"


def test_exception_frames_are_labeled_with_the_scanned_roles():
    trace = one_trace("service_exception_500")

    path = code_path(trace.frames, scan(DEMO_APP).role)

    assert [(f.role, f.frame.class_name, f.frame.method, f.frame.file, f.frame.line) for f in path] == [
        ("controller", "com.demo.controller.UserController", "list", "UserController.java", 26),
        ("service", "com.demo.service.UserService", "findAll", "UserService.java", 20),
    ]
    assert len(trace.frames) > len(path)  # the framework's frames are left out


def test_a_trace_keeps_its_own_lines_from_first_to_last():
    trace = one_trace("service_exception_500")

    assert 'GET "/users", parameters={}' in trace.lines[0]
    assert 'Exiting from "ERROR" dispatch, status 500' in trace.lines[-1]
    assert "\tat com.demo.service.UserService.findAll(UserService.java:20) ~[classes/:na]" in trace.lines
    assert not any("Started DemoAppApplication" in line for line in trace.lines)


def test_a_trace_rebuilt_from_its_lines_is_the_same():
    trace = one_trace("post_missing_body")

    rebuilt = trace_from_lines(REQUEST, trace.lines)

    assert (rebuilt.method, rebuilt.path, rebuilt.status, statuses(rebuilt)) == ("POST", "/users", 400, "✓✓✓✗··✗")
    assert rebuilt.run.failure.details == trace.run.failure.details


def request_lines(log: str, thread: str = "nio-8080-exec-1") -> list[str]:
    """The lines of the request in `log`, moved onto `thread`."""
    return [line.replace("[nio-8080-exec-1]", f"[{thread}]") for line in one_trace(log).lines]


def test_requests_on_different_threads_are_kept_apart():
    first = request_lines("healthy_get_users")
    second = request_lines("display_typo_404", thread="nio-8080-exec-2")
    interleaved = [line for pair in zip(first, second) for line in pair] + first[len(second) :]

    found = traces(interleaved)

    assert sorted((t.thread, t.path, t.status, statuses(t)) for t in found) == [
        ("nio-8080-exec-1", "/users", 200, "✓✓✓✓✓✓✓"),
        ("nio-8080-exec-2", "/display", 404, "✓✓✗···✗"),
    ]


def test_a_request_that_never_ends_is_closed_by_the_next_one_on_its_thread():
    unfinished = request_lines("healthy_get_users")[:-1]

    found = traces(unfinished + request_lines("display_typo_404"))

    assert [(t.path, t.status) for t in found] == [("/users", None), ("/display", 404)]


def test_a_request_still_open_when_the_app_stops_is_closed_without_a_status():
    (trace,) = traces(request_lines("healthy_get_users")[:3])

    assert trace.status is None
    assert statuses(trace) == "✓✓✓✓✓✓·"


def test_thread_names_from_real_lines():
    lines = log_lines("display_typo_404")
    by_text = {
        "Starting DemoAppApplication": "main",
        'GET "/display"': "nio-8080-exec-1",
        "Initializing Spring DispatcherServlet": "nio-8080-exec-1",  # its logger name has [brackets]
    }
    for text, thread in by_text.items():
        line = next(line for line in lines if text in line)
        assert thread_of(line) == thread
    assert thread_of("[INFO] BUILD SUCCESS") is None


def test_thread_name_without_the_application_name():
    # Spring Boot before 3.2, or no spring.application.name.
    line = re.sub(r"\[demo-app\] ", "", next(l for l in log_lines("healthy_get_users") if 'GET "/users"' in l))

    assert thread_of(line) == "nio-8080-exec-1"
