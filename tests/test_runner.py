"""Tests for fsd run, using small fake commands instead of a real Spring Boot app."""

import io
import os
import signal
import subprocess
import sys
import time

import pytest

from fsd import runner

PY = sys.executable
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals")


def fake_app(script: str) -> list[str]:
    return [PY, "-c", script]


def test_output_passes_through_unchanged():
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'Starting DemoApplication using Java 21\\n')\n"
        "sys.stdout.buffer.write(b'\\x1b[32m  INFO\\x1b[0m caf\\xc3\\xa9 \\t tab\\n')\n"
        "sys.stdout.buffer.write(b'Started DemoApplication in 3.1 seconds\\n')\n"
    )
    out = io.BytesIO()

    code = runner.run(fake_app(script), out)

    assert code == 0
    assert out.getvalue() == (
        b"Starting DemoApplication using Java 21\n"
        b"\x1b[32m  INFO\x1b[0m caf\xc3\xa9 \t tab\n"
        b"Started DemoApplication in 3.1 seconds\n"
    )


def test_stderr_is_included_in_order():
    script = (
        "import sys\n"
        "print('one', flush=True)\n"
        "print('two', file=sys.stderr, flush=True)\n"
        "print('three', flush=True)\n"
    )
    out = io.BytesIO()

    runner.run(fake_app(script), out)

    assert out.getvalue().splitlines() == [b"one", b"two", b"three"]


def test_debug_log_env_vars_are_set():
    script = (
        "import os\n"
        "print(os.environ['LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_WEB'])\n"
        "print(os.environ['LOGGING_LEVEL_ORG_HIBERNATE_SQL'])\n"
    )
    out = io.BytesIO()

    runner.run(fake_app(script), out)

    assert out.getvalue().splitlines() == [b"DEBUG", b"DEBUG"]


def test_debug_env_keeps_existing_vars():
    env = runner.debug_env({"PATH": "/bin"})

    assert env["PATH"] == "/bin"
    assert env["LOGGING_LEVEL_ORG_HIBERNATE_SQL"] == "DEBUG"


def test_exit_code_is_passed_on():
    out = io.BytesIO()

    assert runner.run(fake_app("raise SystemExit(3)"), out) == 3


def test_lines_arrive_before_the_app_exits():
    app = runner.App(fake_app("import time\nprint('ready', flush=True)\ntime.sleep(30)"))
    try:
        assert next(app.lines()) == b"ready\n"
        assert app.is_running()
    finally:
        app.kill()


@posix_only
def test_stop_kills_an_app_that_ignores_ctrl_c():
    script = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(30)"
    app = runner.App(fake_app(script))
    next(app.lines())

    app.stop(grace=0.5)

    assert app.process.returncode == -signal.SIGKILL


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.05)
    return False


@posix_only
def test_ctrl_c_stops_the_app_and_the_processes_it_started():
    # Like mvnw forking a JVM: the fake app starts a grandchild and prints its pid.
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    fsd = subprocess.Popen(
        [PY, "-m", "fsd", "run", "--", PY, "-c", script],
        stdout=subprocess.PIPE,
    )
    grandchild_pid = int(fsd.stdout.readline())

    fsd.send_signal(signal.SIGINT)

    assert fsd.wait(timeout=15) == runner.EXIT_INTERRUPTED
    assert wait_until_gone(grandchild_pid)


def test_cli_runs_the_command_after_double_dash():
    result = subprocess.run(
        [PY, "-m", "fsd", "run", "--", PY, "-c", "print('hello from the app')"],
        capture_output=True,
    )

    assert result.returncode == 0
    assert result.stdout == b"hello from the app\n"


def test_cli_reports_a_missing_command():
    result = subprocess.run(
        [PY, "-m", "fsd", "run", "--", "no-such-command-fsd-test"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 127
    assert "command not found: no-such-command-fsd-test" in result.stderr
