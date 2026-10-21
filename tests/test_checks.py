"""Tests for the startup checklists.

Each checklist runs on the details the tracker reads from a real log in
tests/logs/, against the source it came from: a copy of samples/demo-app broken
with the same edit as scripts/capture_logs.py made, or a small fixture project.
"""

import importlib.util
import os
import shutil
import socket
from pathlib import Path

import pytest

from fsd.checks import spring as checks
from fsd.checks.spring import CHECKLISTS, Listener, diagnose, run_checklist
from fsd.scanner.spring import scan
from fsd.tracker import Tracker, load_pipeline

REPO = Path(__file__).parent.parent
LOGS = Path(__file__).parent / "logs"
DEMO_APP = REPO / "samples" / "demo-app"
FIXTURES = Path(__file__).parent / "fixtures" / "checks"
STARTUP = load_pipeline("spring_startup")


def load_capture_cases():
    spec = importlib.util.spec_from_file_location("capture_logs", REPO / "scripts" / "capture_logs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {case.name: case for case in module.CASES}, module.APP


CASES, CAPTURED_APP = load_capture_cases()


def logged_failure(log: str):
    tracker = Tracker(STARTUP)
    for line in (LOGS / f"{log}.log").open(encoding="utf-8"):
        tracker.feed(line)
    tracker.finish()
    return tracker.failure


def broken_demo(case: str, tmp_path: Path) -> Path:
    """A copy of the demo app's source with the same break as when tests/logs/<case>.log was captured."""
    copy = tmp_path / "demo-app"
    shutil.copytree(DEMO_APP / "src", copy / "src")
    for edit in CASES[case].edits:
        edit.apply_to(copy / edit.path.relative_to(CAPTURED_APP))
    return copy


def outcomes(results) -> list[tuple[bool, str]]:
    return [(result.passed, result.message) for result in results]


def test_every_failure_names_a_checklist_that_exists():
    rules = [rule for step in STARTUP.steps for rule in step.failures]

    assert rules
    for rule in rules:
        assert rule.checklist in CHECKLISTS, rule.id


def test_a_checklist_stops_at_the_first_failing_check(tmp_path):
    failure = logged_failure("missing_entity_annotation")

    results = diagnose(failure, broken_demo("missing_entity_annotation", tmp_path))

    assert len(results) == 1
    assert len(CHECKLISTS["not_a_managed_type"]) == 4


def test_unknown_failures_have_no_checks():
    tracker = Tracker(STARTUP)
    tracker.failure = logged_failure("port_in_use")
    tracker.failure.rule = STARTUP.fallback

    assert diagnose(tracker.failure, DEMO_APP) == []


# --- missing bean ---


def test_missing_service_annotation(tmp_path):
    results = diagnose(logged_failure("missing_service_annotation"), broken_demo("missing_service_annotation", tmp_path))

    assert outcomes(results) == [
        (True, "class UserService exists"),
        (False, "UserService has no @Service, @Component or other bean annotation"),
    ]
    assert results[-1].fix == 'add @Service on the line above "public class UserService" (UserService.java:10)'
    assert results[-1].location == "src/main/java/com/demo/service/UserService.java:10"


def test_missing_bean_once_fixed_finds_no_cause():
    results = diagnose(logged_failure("missing_service_annotation"), DEMO_APP)

    assert outcomes(results) == [
        (True, "class UserService exists"),
        (True, "UserService is a bean: @Service"),
        (True, "UserService is in com.demo.service, under com.demo"),
    ]


def test_library_type_that_spring_boot_does_not_create(tmp_path):
    results = diagnose(logged_failure("missing_library_bean"), broken_demo("missing_library_bean", tmp_path))

    assert outcomes(results) == [
        (False, "RestTemplate is not in your source code: it comes from a library (org.springframework.web.client.RestTemplate)")
    ]
    assert "@Bean RestTemplate restTemplate() { return new RestTemplate(); }" in results[0].fix


def test_other_library_type_suggests_a_starter():
    details = dict(logged_failure("missing_library_bean").details, bean_type="org.springframework.mail.javamail.JavaMailSender")

    (result,) = run_checklist("missing_bean", scan(DEMO_APP), details)

    assert result.fix == "add the Spring Boot starter that provides JavaMailSender to pom.xml, or a @Bean method that returns one"


def test_bean_outside_the_application_package():
    results = diagnose(logged_failure("missing_service_annotation"), FIXTURES / "bean_outside_package")

    assert outcomes(results) == [
        (True, "class UserService exists"),
        (True, "UserService is a bean: @Service"),
        (False, "UserService is in com.demo.service, outside com.demo.app where Spring Boot looks for beans"),
    ]
    assert results[-1].fix == (
        "move UserService into com.demo.app or a package below it, "
        'or add scanBasePackages = "com.demo.service" to @SpringBootApplication in DemoAppApplication'
    )


def test_interface_whose_implementation_has_no_annotation():
    results = diagnose(logged_failure("missing_service_annotation"), FIXTURES / "bean_interface")

    assert outcomes(results) == [
        (True, "interface UserService exists"),
        (False, "UserServiceImpl implements UserService but has no bean annotation"),
    ]
    assert results[-1].fix == (
        'add @Service on the line above "public class UserServiceImpl implements UserService" (UserServiceImpl.java:3), '
        "with import org.springframework.stereotype.Service;"
    )


def copy_fixture(name: str, tmp_path: Path) -> Path:
    copy = tmp_path / name
    shutil.copytree(FIXTURES / name, copy)
    return copy


def test_interface_with_no_implementation(tmp_path):
    project = copy_fixture("bean_interface", tmp_path)
    (project / "src/main/java/com/demo/service/UserServiceImpl.java").unlink()

    results = diagnose(logged_failure("missing_service_annotation"), project)

    assert outcomes(results)[-1] == (False, "UserService is an interface and no class implements it")
    assert results[-1].fix == "write a class that implements UserService and add @Service to it"


def test_interface_with_an_annotated_implementation_passes(tmp_path):
    project = copy_fixture("bean_interface", tmp_path)
    impl = project / "src/main/java/com/demo/service/UserServiceImpl.java"
    impl.write_text(impl.read_text().replace("public class", "@org.springframework.stereotype.Service\npublic class"))

    results = diagnose(logged_failure("missing_service_annotation"), project)

    assert outcomes(results)[-1] == (True, "UserServiceImpl implements UserService and is a bean")


# --- missing entity ---


def test_missing_entity_annotation(tmp_path):
    results = diagnose(logged_failure("missing_entity_annotation"), broken_demo("missing_entity_annotation", tmp_path))

    assert outcomes(results) == [(False, "User has no @Entity")]
    assert results[0].fix == 'add @Entity on the line above "public class User" (User.java:14)'


def test_entity_once_fixed_finds_no_cause():
    results = diagnose(logged_failure("missing_entity_annotation"), DEMO_APP)

    assert outcomes(results) == [
        (True, "User has @Entity"),
        (True, "User uses jakarta.persistence, not javax.persistence"),
        (True, "User has an @Id field: id"),
        (True, "User is in com.demo.model, under com.demo"),
    ]


def test_entity_with_javax_imports():
    results = diagnose(logged_failure("missing_entity_annotation"), FIXTURES / "entity_javax")

    assert outcomes(results) == [
        (True, "User has @Entity"),
        (False, "User imports javax.persistence.Entity, which Spring Boot 3 and later ignore"),
    ]
    assert results[-1].fix == "change javax.persistence to jakarta.persistence in the imports of User.java"
    assert results[-1].location == "src/main/java/com/demo/model/User.java:3"


def test_entity_without_id():
    results = diagnose(logged_failure("missing_entity_annotation"), FIXTURES / "entity_without_id")

    assert outcomes(results)[-1] == (False, "User has no @Id field")
    assert results[-1].fix.startswith("add @Id (import jakarta.persistence.Id) above the field")


def test_entity_outside_the_application_package():
    results = diagnose(logged_failure("missing_entity_annotation"), FIXTURES / "entity_outside_package")

    assert outcomes(results)[-1] == (
        False,
        "User is in com.demo.model, outside com.demo.app where Spring Boot looks for entities",
    )
    assert results[-1].fix == (
        'move User into com.demo.app or a package below it, or add @EntityScan("com.demo.model") to DemoAppApplication'
    )


# --- duplicate mapping ---


def test_duplicate_mapping_shows_both_handlers(tmp_path):
    results = diagnose(logged_failure("duplicate_mapping"), broken_demo("duplicate_mapping", tmp_path))

    assert outcomes(results) == [
        (
            False,
            "GET /display is mapped by two methods:\n"
            "DisplayController#display()      src/main/java/com/demo/controller/DisplayController.java:9\n"
            "DisplayController#showDisplay()  src/main/java/com/demo/controller/DisplayController.java:14",
        )
    ]
    assert results[0].fix == "change the path or the HTTP method of one of them, or remove one"


# --- port in use ---


def test_port_in_use_by_another_copy_of_this_app(monkeypatch):
    command = "java -cp target/classes com.demo.DemoAppApplication"
    monkeypatch.setattr(checks, "listening_process", lambda port: Listener(4242, "java", command))

    results = diagnose(logged_failure("port_in_use"), DEMO_APP)

    assert outcomes(results) == [(False, "port 8080 is used by another copy of this app: java (pid 4242)")]
    assert results[0].fix == (
        "stop the other copy (Ctrl+C in its terminal, or kill 4242), "
        "or run this app on another port: add server.port=8081 to src/main/resources/application.properties"
    )


def test_port_in_use_by_another_program(monkeypatch):
    monkeypatch.setattr(checks, "listening_process", lambda port: Listener(7, "python3", "python3 -m http.server 8080"))

    (result,) = diagnose(logged_failure("port_in_use"), DEMO_APP)

    assert result.message == "port 8080 is used by python3 (pid 7)"
    assert result.fix.startswith("stop it (kill 7), or run this app on another port")


def test_port_in_use_when_the_process_cannot_be_told(monkeypatch):
    monkeypatch.setattr(checks, "listening_process", lambda port: None)

    (result,) = diagnose(logged_failure("port_in_use"), DEMO_APP)

    assert result.message == "port 8080 is used by another program, most often another copy of this app still running"


def test_port_fix_points_at_an_existing_server_port(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, "listening_process", lambda port: None)

    (result,) = diagnose(logged_failure("port_in_use"), broken_demo("bad_port_property", tmp_path))

    assert result.fix.endswith("change server.port in src/main/resources/application.properties:2 to a free port, like 8081")


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_listening_process_finds_a_real_listener():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]

        listener = checks.listening_process(port)

    assert listener.pid == os.getpid()
    assert "python" in listener.name.lower()


# --- compile error ---


def test_cannot_find_symbol_shows_the_line_and_similar_methods(tmp_path):
    results = diagnose(logged_failure("compile_error"), broken_demo("compile_error", tmp_path))

    assert outcomes(results) == [
        (
            False,
            "UserService.java:20 cannot find method findEverything()\n"
            "return userRepository.findEverything();\n"
            "                     ^",
        )
    ]
    assert results[0].location == "src/main/java/com/demo/service/UserService.java:20"
    assert results[0].fix == "UserRepository has no method findEverything. Did you mean findById(), findAllById() or findAll()?"


def test_other_compile_errors_show_the_line(tmp_path):
    details = {k: v for k, v in logged_failure("compile_error").details.items() if k not in ("symbol", "location")}

    (result,) = run_checklist("compile_error", scan(broken_demo("compile_error", tmp_path)), details)

    assert result.message.splitlines()[0] == "UserService.java:20 cannot find symbol"
    assert result.fix == "fix the code at UserService.java:20"


def test_unknown_class_suggests_similar_class_names(tmp_path):
    details = dict(logged_failure("compile_error").details, symbol="class UserServise", location="class com.demo.App")

    (result,) = run_checklist("compile_error", scan(broken_demo("compile_error", tmp_path)), details)

    assert result.fix == (
        "there is no class UserServise. Did you mean UserService or PostService? Check the spelling and the import"
    )


# --- bad property ---


def test_bad_property_shows_file_line_type_and_an_example(tmp_path):
    results = diagnose(logged_failure("bad_port_property"), broken_demo("bad_port_property", tmp_path))

    assert outcomes(results) == [(False, "server.port=abc is not a whole number, which Integer needs")]
    assert results[0].location == "src/main/resources/application.properties:2"
    assert results[0].fix == "use a whole number, for example server.port=8080"


def test_bad_property_not_in_the_scanned_files_uses_the_logged_origin():
    (result,) = diagnose(logged_failure("bad_port_property"), DEMO_APP)

    assert result.location == "application.properties:2"


@pytest.mark.parametrize(
    ("target_type", "fix"),
    [
        ("java.lang.Boolean", "use true or false, for example app.flag=true"),
        ("java.time.Duration", "use a duration, for example app.flag=30s"),
        ("com.demo.Mode", "set app.flag to a valid Mode"),
    ],
)
def test_bad_property_example_depends_on_the_type(target_type, fix):
    details = dict(logged_failure("bad_port_property").details, property="app.flag", target_type=target_type)

    (result,) = run_checklist("property_bind_failed", scan(DEMO_APP), details)

    assert result.fix == fix


# --- database refused ---


def test_database_refused_shows_the_url_and_suggests_in_memory_h2(tmp_path):
    results = diagnose(logged_failure("database_refused"), broken_demo("database_refused", tmp_path))

    assert outcomes(results) == [
        (True, "spring.datasource.url = jdbc:h2:tcp://localhost:9999/mem:x"),
        (False, "no database is running at localhost:9999: nothing accepted the connection"),
    ]
    assert results[0].location == "src/main/resources/application.properties:2"
    assert results[1].fix == (
        "start the H2 server on port 9999, or use an in-memory database instead: spring.datasource.url=jdbc:h2:mem:testdb"
    )


def test_database_refused_without_a_url_in_the_properties():
    results = diagnose(logged_failure("database_refused"), DEMO_APP)

    assert outcomes(results) == [(False, "spring.datasource.url is not in application.properties or application.yml")]


def test_database_server_url_names_the_database(tmp_path):
    project = broken_demo("database_refused", tmp_path)
    properties = project / "src/main/resources/application.properties"
    properties.write_text("spring.datasource.url=jdbc:mysql://db.local:3306/shop\n")
    # MySQL's driver reports a refused connection without the host and port.
    details = {k: v for k, v in logged_failure("database_refused").details.items() if k not in ("host", "port")}

    results = run_checklist("connection_refused", scan(project), details)

    assert outcomes(results)[-1] == (False, "no database is running at db.local:3306: nothing accepted the connection")
    assert results[-1].fix == "start MySQL at db.local:3306, or fix the host and port in spring.datasource.url"
