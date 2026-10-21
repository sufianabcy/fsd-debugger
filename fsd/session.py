"""The session file: every request trace fsd run has seen, one JSON line each, for fsd try to read.

It lives in the OS temp folder, named by a hash of the project folder, so fsd
never writes into the user's project.
"""

import hashlib
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from fsd.tracker import Pipeline, Trace, trace_from_lines

POLL_SECONDS = 0.1


def session_file(project: Path) -> Path:
    key = hashlib.sha256(str(Path(project).resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "fsd" / f"session-{key}.jsonl"


def start_session(project: Path) -> Path:
    """Make sure the project's session file exists, so fsd try can tell fsd run is in use here."""
    file = session_file(project)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.touch()
    return file


def trace_record(trace: Trace) -> dict:
    """A finished trace as JSON: a summary, and its log lines to rebuild it from."""
    run = trace.run
    return {
        "method": trace.method,
        "path": trace.path,
        "status": trace.status,
        "thread": trace.thread,
        "finished_at": trace.finished_at,
        "steps": {step.id: run.status[step.id].value for step in run.pipeline.steps},
        "cause": run.failure.rule.id if run.failure else None,
        "lines": trace.lines,
    }


def append_trace(file: Path, trace: Trace) -> None:
    with file.open("a", encoding="utf-8") as out:
        out.write(json.dumps(trace_record(trace)) + "\n")


def trace_from_record(pipeline: Pipeline, record: dict) -> Trace | None:
    trace = trace_from_lines(pipeline, record["lines"])
    if trace is not None:
        trace.finished_at = record["finished_at"]
    return trace


def records_after(file: Path, offset: int) -> tuple[list[dict], int]:
    """The complete records written after byte `offset`, and the offset after the last of them."""
    with file.open("rb") as data:
        data.seek(offset)
        written = data.read()
    complete = written[: written.rfind(b"\n") + 1]
    records = []
    for line in complete.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records, offset + len(complete)


def wait_for_record(file: Path, method: str, path: str, offset: int, since: float, timeout: float) -> dict | None:
    """The first trace of `method` `path` written after byte `offset` and finished after `since`.

    Spring logs the query with the path, so a record's query is left out when comparing.
    """
    deadline = time.monotonic() + timeout
    while True:
        records, offset = records_after(file, offset)
        for record in records:
            same_path = urlsplit(record["path"]).path == path
            if record["method"] == method and same_path and record["finished_at"] >= since:
                return record
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)
