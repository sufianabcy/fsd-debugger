"""Tests for the session file that fsd run writes and fsd try reads."""

import tempfile
import threading
import time
from pathlib import Path

from fsd.session import (
    append_trace,
    records_after,
    session_file,
    start_session,
    trace_from_record,
    trace_record,
    wait_for_record,
)
from fsd.tracker import RequestTracker, Status, load_pipeline

LOGS = Path(__file__).parent / "logs"
REQUEST = load_pipeline("spring_request")


def logged_trace(log: str):
    found = []
    tracker = RequestTracker(REQUEST, found.append)
    for line in (LOGS / f"{log}.log").open(encoding="utf-8"):
        tracker.feed(line)
    tracker.finish()
    return found[0]


def test_session_file_is_in_the_temp_folder_keyed_by_the_project(tmp_path):
    project = tmp_path / "shop"
    project.mkdir()

    file = session_file(project)

    assert file.parent == Path(tempfile.gettempdir()) / "fsd"
    assert project not in file.parents
    assert session_file(project / ".") == file
    assert session_file(tmp_path / "other") != file


def test_start_session_creates_the_file_and_keeps_what_is_there(tmp_path):
    file = start_session(tmp_path)
    file.write_text("kept\n")

    assert start_session(tmp_path) == file
    assert file.read_text() == "kept\n"


def test_a_trace_is_written_as_one_json_line_and_read_back(tmp_path):
    file = start_session(tmp_path)
    trace = logged_trace("display_typo_404")

    append_trace(file, trace)
    (record,), offset = records_after(file, 0)

    assert offset == file.stat().st_size
    assert {k: record[k] for k in ("method", "path", "status", "thread", "cause")} == {
        "method": "GET",
        "path": "/display",
        "status": 404,
        "thread": "nio-8080-exec-1",
        "cause": "no_handler",
    }
    assert record["steps"]["match_controller"] == "failed"
    rebuilt = trace_from_record(REQUEST, record)
    assert rebuilt.run.status["send_response"] is Status.FAILED
    assert rebuilt.finished_at == trace.finished_at


def test_a_half_written_line_is_left_for_later(tmp_path):
    file = start_session(tmp_path)
    append_trace(file, logged_trace("healthy_get_users"))
    with file.open("a") as out:
        out.write('{"method": "GET", "pa')

    records, offset = records_after(file, 0)

    assert len(records) == 1
    assert offset < file.stat().st_size


def record_now(file: Path, log: str) -> None:
    trace = logged_trace(log)
    trace.finished_at = time.time()
    append_trace(file, trace)


def test_wait_finds_a_trace_written_while_waiting(tmp_path):
    file = start_session(tmp_path)
    since = time.time()
    threading.Timer(0.3, record_now, (file, "healthy_get_users")).start()

    record = wait_for_record(file, "GET", "/users", 0, since, timeout=3)

    assert record["status"] == 200


def test_wait_skips_other_requests_and_older_traces(tmp_path):
    file = start_session(tmp_path)
    record_now(file, "healthy_get_users")  # before the request was sent
    offset = file.stat().st_size
    since = time.time()
    record_now(file, "display_typo_404")  # another path

    assert wait_for_record(file, "GET", "/users", offset, since, timeout=0.3) is None
    assert wait_for_record(file, "GET", "/users", 0, since, timeout=0.3) is None
    assert wait_for_record(file, "GET", "/display", offset, since, timeout=0.3)["status"] == 404


def test_trace_record_has_every_step(tmp_path):
    record = trace_record(logged_trace("service_exception_500"))

    assert record["steps"] == {
        "reach_app": "passed",
        "pass_filters": "passed",
        "match_controller": "passed",
        "read_request": "passed",
        "run_code": "failed",
        "run_sql": "waiting",
        "send_response": "failed",
    }
    assert record["cause"] == "exception"
