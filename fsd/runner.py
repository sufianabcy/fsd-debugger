"""Start the user's app as a child process and stream its output line by line."""

import os
import signal
import subprocess
import threading
from collections.abc import Iterator
from typing import BinaryIO

# Spring Boot reads these on its own, so the app logs every request and SQL
# statement without any change to application.properties.
DEBUG_LOG_ENV = {
    "LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_WEB": "DEBUG",
    "LOGGING_LEVEL_ORG_HIBERNATE_SQL": "DEBUG",
}

STOP_GRACE_SECONDS = 10.0
EXIT_INTERRUPTED = 130
IS_WINDOWS = os.name == "nt"


def debug_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment with Spring's debug log levels switched on."""
    env = dict(os.environ if base is None else base)
    env.update(DEBUG_LOG_ENV)
    return env


def own_process_group() -> dict:
    """Popen options that put the app in its own process group.

    Then one signal reaches the app and everything it starts, such as the JVM
    that `mvnw spring-boot:run` forks.
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"process_group": 0}


class App:
    """A running child process whose stdout and stderr arrive as one stream of lines."""

    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=debug_env(),
            **own_process_group(),
        )

    def lines(self) -> Iterator[bytes]:
        """Yield each output line as raw bytes, newline included, as soon as it arrives."""
        yield from self.process.stdout

    def is_running(self) -> bool:
        return self.process.poll() is None

    def interrupt(self) -> None:
        """Ask the app and its children to shut down, like Ctrl+C in a terminal."""
        if not self.is_running():
            return
        if IS_WINDOWS:
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(self.process.pid, signal.SIGINT)

    def kill(self) -> None:
        """Stop the app and its children at once, even children that outlived the app."""
        if IS_WINDOWS:
            if self.is_running():
                self.process.kill()
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # the whole group has already exited

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        """Interrupt the app, and kill it if it is still running after `grace` seconds."""
        self.interrupt()
        timer = threading.Timer(grace, self.kill)
        timer.daemon = True
        timer.start()
        self.process.wait()
        timer.cancel()

    def wait(self) -> int:
        return self.process.wait()


def pass_through(lines: Iterator[bytes], out: BinaryIO) -> None:
    """Write each line to `out` exactly as received."""
    for line in lines:
        out.write(line)
        out.flush()


def run(command: list[str], out: BinaryIO, grace: float = STOP_GRACE_SECONDS) -> int:
    """Run `command`, copy its output to `out` unchanged, and return its exit code.

    On Ctrl+C the app is stopped too, and its shutdown messages still pass through.
    A second Ctrl+C kills it at once.
    """
    app = App(command)
    try:
        pass_through(app.lines(), out)
        return app.wait()
    except KeyboardInterrupt:
        stopper = threading.Thread(target=app.stop, args=(grace,), daemon=True)
        stopper.start()
        try:
            pass_through(app.lines(), out)
        except KeyboardInterrupt:
            pass
        app.kill()
        app.wait()
        return EXIT_INTERRUPTED
    finally:
        if app.is_running():
            app.kill()
