"""Tests for fsd doctor: the source-only checks, on the demo app, the check fixtures and broken copies of the demo app.

A copy broken the way scripts/capture_logs.py broke the demo app for a log in
tests/logs/ must give the same cause and fix before the app starts as fsd run
finds in that log.
"""

import importlib.util
import shutil
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fsd.checks import doctor
from fsd.checks.doctor import examine, typo_segment
from fsd.checks.spring import diagnose
from fsd.cli import app
from fsd.tracker import Tracker, load_pipeline

REPO = Path(__file__).parent.parent
LOGS = Path(__file__).parent / "logs"
DEMO_APP = REPO / "samples" / "demo-app"
FIXTURES = Path(__file__).parent / "fixtures" / "checks"
CONTROLLERS = Path("src/main/java/com/demo/controller")
PROPERTIES = Path("src/main/resources/application.properties")
REAL_PORT_CHECK = doctor.port_accepts_connections


def load_capture_cases():
    spec = importlib.util.spec_from_file_location("capture_logs", REPO / "scripts" / "capture_logs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {case.name: case for case in module.CASES}, module.APP


CASES, CAPTURED_APP = load_capture_cases()


@pytest.fixture(autouse=True)
def nothing_listens(monkeypatch):
    """Whatever runs on this machine's port 8080 must not decide the tests."""
    monkeypatch.setattr(doctor, "port_accepts_connections", lambda port: False)


def demo_copy(tmp_path: Path, case: str | None = None) -> Path:
    """A copy of the demo app's source and pom.xml, broken like `case` was for its log."""
    copy = tmp_path / "demo-app"
    shutil.copytree(DEMO_APP / "src", copy / "src")
    shutil.copy(DEMO_APP / "pom.xml", copy / "pom.xml")
    for edit in CASES[case].edits if case else []:
        edit.apply_to(copy / edit.path.relative_to(CAPTURED_APP))
    return copy


def edit(file: Path, old: str, new: str) -> None:
    text = file.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    file.write_text(text.replace(old, new), encoding="utf-8")


def add_mapping(project: Path, controller: str, annotation: str, name: str) -> None:
    edit(
        project / CONTROLLERS / f"{controller}.java",
        "\n}\n",
        f'\n\n\t{annotation}\n\tpublic String {name}() {{\n\t\treturn "";\n\t}}\n}}\n',
    )


def problems(project: Path):
    return [r for section in examine(project) for r in section.results if not r.passed]


def messages(project: Path) -> list[str]:
    return [r.message for r in problems(project)]


def logged_failure(log: str):
    tracker = Tracker(load_pipeline("spring_startup"))
    for line in (LOGS / f"{log}.log").open(encoding="utf-8"):
        tracker.feed(line)
    tracker.finish()
    return tracker.failure


# --- the command ---


def test_healthy_demo_app_passes_every_check():
    result = CliRunner().invoke(app, ["doctor", str(DEMO_APP)])

    assert result.exit_code == 0
    assert "✗" not in result.output
    assert "no problems found" in result.output
    for section in ("Beans", "Mappings", "Entities", "Database", "Port"):
        assert section in result.output


def test_a_problem_is_shown_with_its_fix_and_exits_1():
    result = CliRunner().invoke(app, ["doctor", str(FIXTURES / "entity_without_id")])
    lines = [" ".join(line.split()) for line in result.output.splitlines()]

    assert result.exit_code == 1
    assert "✗ User has no @Id field" in lines
    assert "src/main/java/com/demo/model/User.java:6" in lines
    assert any(line.startswith("Fix: add @Id (import jakarta.persistence.Id)") for line in lines)
    assert lines[-1] == "✗ 1 problem found, 2 checks passed"


def test_a_folder_without_java_source_is_not_a_project(tmp_path):
    result = CliRunner().invoke(app, ["doctor", str(tmp_path)])

    assert result.exit_code == 2
    assert "no src/main/java in" in result.output


# --- the check fixtures ---


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("bean_interface", ["UserController asks for UserService: UserServiceImpl implements UserService but has no bean annotation"]),
        (
            "bean_outside_package",
            [
                "UserController is in com.demo.controller, outside com.demo.app where Spring Boot looks for beans",
                "UserService is in com.demo.service, outside com.demo.app where Spring Boot looks for beans",
            ],
        ),
        ("entity_javax", ["User imports javax.persistence.Entity, which Spring Boot 3 and later ignore"]),
        ("entity_outside_package", ["User is in com.demo.model, outside com.demo.app where Spring Boot looks for entities"]),
        ("entity_without_id", ["User has no @Id field"]),
    ],
)
def test_each_broken_fixture_shows_its_problem(fixture, expected):
    assert messages(FIXTURES / fixture) == expected


def test_scan_base_packages_counts_as_scanned(tmp_path):
    project = tmp_path / "app"
    shutil.copytree(FIXTURES / "bean_outside_package", project)
    edit(project / "src/main/java/com/demo/app/DemoAppApplication.java", "@SpringBootApplication", '@SpringBootApplication(scanBasePackages = "com.demo")')

    assert messages(project) == []


def test_a_project_without_a_spring_boot_application_class(tmp_path):
    project = tmp_path / "app"
    shutil.copytree(FIXTURES / "entity_without_id", project)
    (project / "src/main/java/com/demo/DemoAppApplication.java").unlink()

    assert messages(project)[0] == "no class in src/main/java has @SpringBootApplication"


# --- the same cause as fsd run finds in the log ---


@pytest.mark.parametrize(
    "case", ["missing_service_annotation", "missing_library_bean", "missing_entity_annotation", "duplicate_mapping", "bad_port_property"]
)
def test_doctor_finds_before_startup_what_fsd_run_finds_in_the_log(case, tmp_path):
    project = demo_copy(tmp_path, case)
    from_log = diagnose(logged_failure(case), project)[-1]

    (found,) = problems(project)

    assert from_log.message in found.message
    assert found.fix == from_log.fix
    assert found.location == from_log.location


def test_missing_bean_names_the_class_that_asks(tmp_path):
    (found,) = problems(demo_copy(tmp_path, "missing_service_annotation"))

    assert found.message == "UserController asks for UserService: UserService has no @Service, @Component or other bean annotation"


def test_a_repository_of_a_class_without_entity(tmp_path):
    assert messages(demo_copy(tmp_path, "missing_entity_annotation")) == ["User has no @Entity, but UserRepository stores it"]


# --- mappings ---


def test_paths_that_differ_by_a_typo(tmp_path):
    project = demo_copy(tmp_path)
    add_mapping(project, "DisplayController", '@GetMapping("/disply")', "other")

    (found,) = problems(project)

    assert found.message.splitlines()[0] == "/display and /disply are 1 letter apart, which is often a typo:"
    assert found.fix == 'if they are meant to be one path, change @GetMapping("/disply") to @GetMapping("/display") (DisplayController.java:15)'


def test_the_spelling_most_mappings_use_is_kept(tmp_path):
    project = demo_copy(tmp_path)
    add_mapping(project, "PostController", '@GetMapping("/user/{id}/posts")', "byUser")

    (found,) = problems(project)

    assert found.message.startswith("/users/{userId}/posts and /user/{id}/posts are 1 letter apart")
    assert found.fix.startswith('if they are meant to be one path, change @GetMapping("/user/{id}/posts") to @GetMapping("/users/{id}/posts")')


def test_paths_that_differ_by_the_slash_at_the_end(tmp_path):
    project = demo_copy(tmp_path)
    add_mapping(project, "PostController", '@GetMapping("/posts/")', "listAgain")

    (found,) = problems(project)

    assert found.message.splitlines()[0] == "/posts and /posts/ differ only by the / at the end:"
    assert found.fix.startswith('if they are meant to be one URL, change @GetMapping("/posts/") to @GetMapping("/posts")')
    assert found.note.startswith("since Spring Boot 3")


def test_both_spellings_in_one_annotation_are_on_purpose(tmp_path):
    project = demo_copy(tmp_path)
    edit(project / CONTROLLERS / "PostController.java", '@GetMapping("/posts")', '@GetMapping({"/posts", "/posts/"})')

    assert messages(project) == []


def test_path_variables_with_other_names_are_the_same_mapping(tmp_path):
    project = demo_copy(tmp_path)
    add_mapping(project, "UserController", '@GetMapping("/{userId}")', "byId")

    (found,) = problems(project)

    assert found.message.splitlines()[0] == "GET /users/{id} is mapped by two methods:"


def test_params_make_a_mapping_narrower(tmp_path):
    project = demo_copy(tmp_path)
    add_mapping(project, "DisplayController", '@GetMapping(value = "/display", params = "verbose")', "verbose")

    assert messages(project) == []


@pytest.mark.parametrize(
    ("path", "other", "expected"),
    [
        ("/display", "/disply", (0, 1)),
        ("/users/{}", "/user/{}", (0, 1)),
        ("/api/v1/users", "/api/v2/users", None),
        ("/posts/{}/follow", "/posts/{}/unfollow", None),
        ("/users/{}", "/users/me", None),
        ("/on", "/off", None),
        ("/users", "/posts", None),
    ],
)
def test_which_paths_look_like_a_typo(path, other, expected):
    assert typo_segment(path, other) == expected


# --- database ---


def test_jpa_without_a_database(tmp_path):
    project = demo_copy(tmp_path)
    edit(project / "pom.xml", "<artifactId>h2</artifactId>", "<artifactId>h2-is-gone</artifactId>")

    (found,) = problems(project)

    assert found.message == (
        "pom.xml has spring-boot-starter-data-jpa, and no database is set up: no spring.datasource.url, and no in-memory database such as H2"
    )
    assert found.location == "pom.xml:37"
    assert "<artifactId>h2</artifactId><scope>runtime</scope>" in found.fix


def test_a_datasource_url_in_a_profile_file_counts(tmp_path):
    project = demo_copy(tmp_path)
    edit(project / "pom.xml", "<artifactId>h2</artifactId>", "<artifactId>h2-is-gone</artifactId>")
    (project / "src/main/resources/application-dev.properties").write_text("spring.datasource.url=jdbc:postgresql://localhost/shop\n")

    assert messages(project) == []


def test_jpa_without_a_database_in_gradle(tmp_path):
    project = demo_copy(tmp_path)
    (project / "pom.xml").unlink()
    (project / "build.gradle").write_text(
        "dependencies {\n"
        "    implementation 'org.springframework.boot:spring-boot-starter-data-jpa'\n"
        "    testRuntimeOnly 'com.h2database:h2'\n"
        "}\n"
    )

    (found,) = problems(project)

    assert found.location == "build.gradle:2"
    assert found.fix.endswith("add H2 to build.gradle: runtimeOnly 'com.h2database:h2'")


# --- port ---


def test_port_that_another_program_listens_on(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "port_accepts_connections", REAL_PORT_CHECK)
    project = demo_copy(tmp_path)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        edit(project / PROPERTIES, "spring.application.name=demo-app\n", f"spring.application.name=demo-app\nserver.port={port}\n")

        (found,) = problems(project)

    assert found.message.startswith(f"port {port} is used by")
    assert found.fix.endswith(f"change server.port in src/main/resources/application.properties:2 to a free port, like {port + 1}")


def test_port_accepts_connections_only_while_something_listens():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert REAL_PORT_CHECK(port)
    assert not REAL_PORT_CHECK(port)


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("server.port=0", "server.port=0: Spring Boot picks a free port each time"),
        ("server.port=${PORT:9090}", "port 9090 is free (server.port)"),
        ("server.port=${PORT}", "server.port = ${PORT}, set when the app starts"),
    ],
)
def test_server_port_settings(setting, message, tmp_path):
    project = demo_copy(tmp_path)
    edit(project / PROPERTIES, "spring.application.name=demo-app\n", f"spring.application.name=demo-app\n{setting}\n")

    port = next(section for section in examine(project) if section.title == "Port")

    assert [r.message for r in port.results] == [message]
