"""Capture real Spring Boot logs from samples/demo-app into tests/logs/.

Each case breaks the demo app on purpose, runs it with the same debug log
settings as `fsd run`, saves the full output to tests/logs/<case>.log, and
restores the app, even if the run fails or is interrupted.

    python scripts/capture_logs.py                  # list the cases
    python scripts/capture_logs.py port_in_use      # capture one or more cases
    python scripts/capture_logs.py --all
"""

import argparse
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from fsd.runner import debug_env

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "samples" / "demo-app"
LOGS = ROOT / "tests" / "logs"
SOURCE = APP / "src" / "main" / "java" / "com" / "demo"
PROPERTIES = APP / "src" / "main" / "resources" / "application.properties"
SCHEMA = APP / "src" / "main" / "resources" / "schema.sql"
START = ["./mvnw", "spring-boot:run"]
PORT = 8080
START_TIMEOUT = 180
STOP_TIMEOUT = 30
JSON = "-X POST -H 'Content-Type: application/json'"
BROWSER = (
    "-H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'"
)


@dataclass
class Edit:
    """Replace `old` with `new` in `path`. `old` must occur exactly once; an empty `old` creates the file."""

    path: Path
    old: str
    new: str

    def apply_to(self, target: Path) -> None:
        """Make the edit in `target`: the demo app's own file, or the same file in a copy of it."""
        if not self.old:
            if target.exists():
                raise SystemExit(f"{target.name} already exists; is the demo app already broken?")
            target.write_text(self.new, encoding="utf-8")
            return
        current = target.read_text(encoding="utf-8")
        if current.count(self.old) != 1:
            raise SystemExit(f"{target.name}: expected {self.old!r} once; is the demo app already broken?")
        target.write_text(current.replace(self.old, self.new), encoding="utf-8")


@dataclass
class Case:
    name: str
    edits: list[Edit] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)  # curl arguments, sent once the app has started
    port_taken: bool = False  # run a first copy of the app on the port before capturing


def append_property(line: str) -> Edit:
    return Edit(PROPERTIES, "spring.application.name=demo-app\n", f"spring.application.name=demo-app\n{line}\n")


# A schema written by hand, as with a real database: Hibernate leaves it alone, and it has no posts table.
OWN_SCHEMA = [
    Edit(
        SCHEMA,
        "",
        "create table users (id bigint primary key, name varchar(255) unique);\n"
        "create sequence users_seq start with 1 increment by 50;\n",
    ),
    append_property("spring.jpa.hibernate.ddl-auto=none"),
]
ADD_USER = f"""{JSON} -d '{{"name": "Ada"}}' http://localhost:{PORT}/users"""


CASES = [
    Case("healthy_startup"),
    Case(
        "missing_service_annotation",
        [Edit(SOURCE / "service" / "UserService.java", "@Service\npublic class UserService", "public class UserService")],
    ),
    Case(
        "missing_library_bean",
        [
            Edit(
                SOURCE / "controller" / "DisplayController.java",
                "public class DisplayController {\n",
                "public class DisplayController {\n\n"
                "\tpublic DisplayController(org.springframework.web.client.RestTemplate restTemplate) {\n\t}\n",
            )
        ],
    ),
    Case("port_in_use", port_taken=True),
    Case("missing_entity_annotation", [Edit(SOURCE / "model" / "User.java", "@Entity\n", "")]),
    Case(
        "duplicate_mapping",
        [
            Edit(
                SOURCE / "controller" / "DisplayController.java",
                '\t\treturn "Hello from DisplayController";\n\t}\n',
                '\t\treturn "Hello from DisplayController";\n\t}\n\n'
                '\t@GetMapping("/display")\n\tpublic String showDisplay() {\n\t\treturn "Another display";\n\t}\n',
            )
        ],
    ),
    Case(
        "display_typo_404",
        [Edit(SOURCE / "controller" / "DisplayController.java", '@GetMapping("/display")', '@GetMapping("/disply/")')],
        requests=[f"http://localhost:{PORT}/display"],
    ),
    Case("healthy_get_users", requests=[f"http://localhost:{PORT}/users"]),
    Case(
        "service_exception_500",
        [
            Edit(
                SOURCE / "service" / "UserService.java",
                "return userRepository.findAll();",
                'throw new IllegalStateException("findAll is broken on purpose");',
            )
        ],
        requests=[f"http://localhost:{PORT}/users"],
    ),
    Case(
        "post_missing_body",
        requests=[f"-X POST -H 'Content-Type: application/json' http://localhost:{PORT}/users"],
    ),
    Case("trailing_slash_404", requests=[f"http://localhost:{PORT}/users/"]),
    Case("singular_path_404", requests=[f"http://localhost:{PORT}/user/1"]),
    Case("wrong_method_405", requests=[f"-X DELETE http://localhost:{PORT}/users/1"]),
    Case("post_malformed_json", requests=[f"""{JSON} -d '{{"name": "Ada"' http://localhost:{PORT}/users"""]),
    Case("post_wrong_field_type", requests=[f"""{JSON} -d '{{"id": "abc", "name": "Ada"}}' http://localhost:{PORT}/users"""]),
    Case("path_variable_type_400", requests=[f"http://localhost:{PORT}/users/abc"]),
    Case("missing_request_param_400", requests=[f"http://localhost:{PORT}/users/search"]),
    Case("query_param_type_400", requests=[f"'http://localhost:{PORT}/users/search?name=Ada&limit=ten'"]),
    Case(
        "missing_column_sql",
        [*OWN_SCHEMA, Edit(SOURCE / "model" / "User.java", "\tprivate String name;\n", "\tprivate String name;\n\n\tprivate String email;\n")],
        requests=[f"http://localhost:{PORT}/users"],
    ),
    Case("missing_table_sql", OWN_SCHEMA, requests=[f"http://localhost:{PORT}/posts"]),
    Case("duplicate_key_sql", requests=[ADD_USER, ADD_USER]),
    Case(
        "lazy_initialization",
        [append_property("spring.jpa.open-in-view=false")],
        requests=[ADD_USER, f"http://localhost:{PORT}/users"],
    ),
    Case(
        "json_recursion",
        [Edit(SOURCE / "model" / "Post.java", "\t@JsonIgnore\n", "")],
        requests=[
            ADD_USER,
            f"""{JSON} -d '{{"title": "Hello"}}' http://localhost:{PORT}/users/1/posts""",
            f"http://localhost:{PORT}/users",
        ],
    ),
    Case(
        "browser_favicon_404",
        requests=[
            f"{BROWSER} -H 'Accept: text/html,application/xhtml+xml,*/*;q=0.8' http://localhost:{PORT}/display",
            f"{BROWSER} -H 'Accept: image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8' "
            f"-H 'Referer: http://localhost:{PORT}/display' http://localhost:{PORT}/favicon.ico",
        ],
    ),
    Case(
        "compile_error",
        [
            Edit(
                SOURCE / "service" / "UserService.java",
                "return userRepository.findAll();",
                "return userRepository.findEverything();",
            )
        ],
    ),
    Case("bad_port_property", [append_property("server.port=abc")]),
    Case("database_refused", [append_property("spring.datasource.url=jdbc:h2:tcp://localhost:9999/mem:x")]),
]


def apply_edits(edits: list[Edit]) -> dict[Path, str | None]:
    """Apply `edits` and return the original text of every file they touched: None for a file they created."""
    originals: dict[Path, str | None] = {}
    try:
        for edit in edits:
            originals.setdefault(edit.path, edit.path.read_text(encoding="utf-8") if edit.path.exists() else None)
            edit.apply_to(edit.path)
    except BaseException:
        restore(originals)
        raise
    return originals


def restore(originals: dict[Path, str | None]) -> None:
    for path, text in originals.items():
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")


def port_is_open(port: int) -> bool:
    try:
        socket.create_connection(("localhost", port), timeout=1).close()
        return True
    except OSError:
        return False


def start_app(output) -> subprocess.Popen:
    return subprocess.Popen(
        START, cwd=APP, env=debug_env(), stdin=subprocess.DEVNULL,
        stdout=output, stderr=subprocess.STDOUT, process_group=0,
    )


def stop_app(app: subprocess.Popen) -> None:
    """Ctrl+C the app and everything it started; kill it if it hangs."""
    if app.poll() is None:
        os.killpg(app.pid, signal.SIGINT)
    try:
        app.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(app.pid, signal.SIGKILL)
        app.wait()


def has_started(log: Path) -> bool:
    text = log.read_text(encoding="utf-8", errors="replace")
    return " Started " in text and " seconds (process running for " in text


def wait_for_start_or_exit(app: subprocess.Popen, log: Path) -> bool:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline and app.poll() is None:
        if has_started(log):
            return True
        time.sleep(0.5)
    return False


def take_port() -> subprocess.Popen:
    """Run a first copy of the app, as when it is still running in another terminal."""
    first = start_app(subprocess.DEVNULL)
    deadline = time.monotonic() + START_TIMEOUT
    while not port_is_open(PORT):
        if first.poll() is not None or time.monotonic() > deadline:
            stop_app(first)
            raise SystemExit(f"the first copy of the app never opened port {PORT}")
        time.sleep(0.5)
    return first


def send(request: str) -> None:
    result = subprocess.run(["curl", "-s", "-i", *shlex.split(request)], capture_output=True, text=True)
    status = result.stdout.splitlines()[0] if result.stdout else f"no response (curl exit {result.returncode})"
    print(f"  curl {request} -> {status}")


def capture(case: Case) -> None:
    if port_is_open(PORT):
        raise SystemExit(f"port {PORT} is already in use; stop whatever is running there first")
    log = LOGS / f"{case.name}.log"
    originals = apply_edits(case.edits)
    first = take_port() if case.port_taken else None
    try:
        with log.open("wb") as output:
            app = start_app(output)
            try:
                started = wait_for_start_or_exit(app, log)
                if started:
                    time.sleep(1)
                    for request in case.requests:
                        send(request)
                        time.sleep(1)
            finally:
                stop_app(app)
    finally:
        if first is not None:
            stop_app(first)
        restore(originals)
    print(f"{case.name}: started={started} exit={app.returncode} -> {log.relative_to(ROOT)}")


def main() -> None:
    names = [case.name for case in CASES]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases", nargs="*", metavar="case")
    parser.add_argument("--all", action="store_true", help="capture every case")
    args = parser.parse_args()
    unknown = set(args.cases) - set(names)
    if unknown:
        parser.error(f"unknown case: {', '.join(sorted(unknown))}")
    chosen = names if args.all else args.cases
    if not chosen:
        print("cases:\n  " + "\n  ".join(names))
        return
    for case in CASES:
        if case.name in chosen:
            capture(case)


if __name__ == "__main__":
    sys.exit(main())
