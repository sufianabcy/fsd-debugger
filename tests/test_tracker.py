"""Tests for the startup pipeline and tracker, using the real logs in tests/logs/."""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from fsd.tracker import PIPELINES, EventGrouper, Status, Tracker, load_pipeline

LOGS = Path(__file__).parent / "logs"
STARTUP = load_pipeline("spring_startup")
STEP_NAMES = [
    "Build",
    "Start the app",
    "Load configuration",
    "Prepare web server",
    "Connect to database",
    "Set up entities",
    "Create beans",
    "Register endpoints",
    "Open the port",
    "Ready",
]


def log_lines(name: str) -> list[str]:
    return (LOGS / f"{name}.log").read_text(encoding="utf-8").splitlines(keepends=True)


def track(name: str, pipeline=STARTUP) -> Tracker:
    tracker = Tracker(pipeline)
    for line in log_lines(name):
        tracker.feed(line)
    tracker.finish()
    return tracker


def statuses(tracker: Tracker) -> list[str]:
    symbol = {Status.PASSED: "✓", Status.FAILED: "✗", Status.WAITING: "·"}
    return [symbol[tracker.status[step.id]] for step in tracker.pipeline.steps]


def events(name: str) -> list[str]:
    grouper = EventGrouper()
    found = [event for line in log_lines(name) if (event := grouper.add(line.rstrip("\n"))) is not None]
    return found + [grouper.flush()]


def test_pipeline_has_the_ten_startup_steps_in_order():
    assert [step.name for step in STARTUP.steps] == STEP_NAMES


def test_patterns_never_hard_code_main_class_java_version_or_port():
    text = (PIPELINES / "spring_startup.yaml").read_text(encoding="utf-8")
    patterns = re.findall(r"^\s*- '(.*)'$", text, re.MULTILINE)

    assert patterns
    for pattern in patterns:
        assert "DemoApp" not in pattern
        assert "8080" not in pattern
        assert not re.search(r"Java \d", pattern)


# --- every startup log ends at the right step ---


@pytest.mark.parametrize("log", ["healthy_startup", "display_typo_404", "post_missing_body"])
def test_healthy_startup_ticks_all_ten_steps(log):
    tracker = track(log)

    assert statuses(tracker) == ["✓"] * 10
    assert tracker.failure is None


def test_missing_service_fails_at_create_beans():
    tracker = track("missing_service_annotation")

    assert statuses(tracker) == list("✓✓✓✓✓✓✗···")
    assert tracker.failure.step.name == "Create beans"
    assert tracker.failure.rule.id == "missing_bean"
    assert tracker.failure.details["bean_type"] == "com.demo.service.UserService"
    assert tracker.failure.details["consumer"] == "com.demo.controller.UserController"
    assert tracker.failure.details["injection_point"] == "Parameter 0 of constructor"


def test_library_type_without_a_bean_fails_at_create_beans():
    tracker = track("missing_library_bean")

    assert statuses(tracker) == list("✓✓✓✓✓✓✗···")
    assert tracker.failure.details["bean_type"] == "org.springframework.web.client.RestTemplate"
    assert tracker.failure.details["consumer"] == "com.demo.controller.DisplayController"


def test_port_in_use_fails_at_open_the_port():
    tracker = track("port_in_use")

    assert statuses(tracker) == list("✓✓✓✓✓✓✓✓✗·")
    assert tracker.failure.step.name == "Open the port"
    assert tracker.failure.rule.id == "port_in_use"
    assert tracker.failure.details["port"] == "8080"


def test_missing_entity_fails_at_set_up_entities():
    # "Initialized JPA EntityManagerFactory" is logged before the failure, and the
    # cause only shows deep in a Caused by chain that starts as a bean error.
    tracker = track("missing_entity_annotation")

    assert statuses(tracker) == list("✓✓✓✓✓✗····")
    assert tracker.failure.step.name == "Set up entities"
    assert tracker.failure.rule.id == "not_a_managed_type"
    assert tracker.failure.details["class_name"] == "com.demo.model.User"


def test_duplicate_mapping_fails_at_register_endpoints():
    tracker = track("duplicate_mapping")

    assert statuses(tracker) == list("✓✓✓✓✓✓✓✗··")
    assert tracker.failure.step.name == "Register endpoints"
    assert tracker.failure.rule.id == "ambiguous_mapping"
    assert tracker.failure.details == {
        "bean": "displayController",
        "method": "com.demo.controller.DisplayController#display()",
        "mapping": "GET [/display]",
        "http_method": "GET",
        "path": "/display",
        "existing_bean": "displayController",
        "existing_method": "com.demo.controller.DisplayController#showDisplay()",
    }


def test_compile_error_fails_at_build():
    tracker = track("compile_error")

    assert statuses(tracker) == list("✗·········")
    assert tracker.failure.rule.id == "cannot_find_symbol"
    assert tracker.failure.details["file"].endswith("/src/main/java/com/demo/service/UserService.java")
    assert tracker.failure.details["file_name"] == "UserService.java"
    assert tracker.failure.details["line"] == "20"
    assert tracker.failure.details["column"] == "38"
    assert tracker.failure.details["symbol"] == "method findEverything()"
    assert tracker.failure.details["location"] == "variable userRepository of type com.demo.repository.UserRepository"


def test_any_other_compile_error_is_still_a_build_failure():
    rule = next(rule for rule in STARTUP.steps[0].failures if rule.id == "compile_error")
    event = next(event for event in events("compile_error") if "cannot find symbol" in event)

    details = rule.match(event)

    assert details["file_name"] == "UserService.java"
    assert details["line"] == "20"
    assert details["error"] == "cannot find symbol"


def test_bad_property_fails_at_load_configuration():
    # "No active profile set" is logged first, so the step had already passed.
    tracker = track("bad_port_property")

    assert statuses(tracker) == list("✓✓✗·······")
    assert tracker.failure.step.name == "Load configuration"
    assert tracker.failure.rule.id == "property_bind_failed"
    assert tracker.failure.details == {
        "property": "server.port",
        "target_type": "java.lang.Integer",
        "value": "abc",
        "origin": "class path resource [application.properties]",
        "origin_line": "2",
        "reason": 'failed to convert java.lang.String to java.lang.Integer '
        '(caused by java.lang.NumberFormatException: For input string: "abc")',
    }


def test_database_connection_refused_fails_at_connect_to_database():
    # The final error box blames the Hibernate dialect; the refused connection comes first.
    tracker = track("database_refused")

    assert statuses(tracker) == list("✓✓✓✓✗·····")
    assert tracker.failure.step.name == "Connect to database"
    assert tracker.failure.rule.id == "connection_refused"
    assert tracker.failure.details == {"host": "localhost", "port": "9999"}


def test_connection_refused_outside_jdbc_is_not_blamed_on_the_database():
    rule = next(rule for rule in STARTUP.steps[4].failures if rule.id == "connection_refused")
    refused = next(event for event in events("database_refused") if "Connection refused: localhost" in event)

    assert rule.match(refused) is not None
    assert rule.match("I/O error on GET request: java.net.ConnectException: Connection refused") is None


# --- details from passed lines ---


def test_passed_lines_keep_their_details():
    tracker = track("healthy_startup")

    assert tracker.details["start_app"] == {"main_class": "DemoAppApplication", "java_version": "21.0.9"}
    assert tracker.details["prepare_web_server"] == {"port": "8080"}
    assert tracker.details["register_endpoints"] == {"count": "5"}
    assert tracker.details["open_port"] == {"port": "8080"}
    assert tracker.details["ready"]["main_class"] == "DemoAppApplication"


def test_create_beans_passes_when_a_later_step_passes():
    # Create beans has no log line of its own.
    tracker = Tracker(STARTUP)
    for line in log_lines("healthy_startup"):
        tracker.feed(line)
        if "mappings in 'requestMappingHandlerMapping'" in line:
            break

    assert tracker.status["create_beans"] is Status.PASSED
    assert tracker.status["register_endpoints"] is Status.PASSED
    assert tracker.status["open_port"] is Status.WAITING


def test_ready_passes_as_soon_as_its_line_arrives():
    tracker = Tracker(STARTUP)
    for line in log_lines("healthy_startup"):
        tracker.feed(line)
        if " Started " in line:
            break

    assert tracker.all_passed


def test_colored_output_is_tracked_like_plain_output():
    tracker = Tracker(STARTUP)
    for line in log_lines("missing_service_annotation"):
        tracker.feed(line.replace(" INFO ", " \x1b[32mINFO\x1b[0m ").replace("ERROR", "\x1b[31mERROR\x1b[0m"))
    tracker.finish()

    assert statuses(tracker) == list("✓✓✓✓✓✓✗···")


# --- unknown failures ---


def without_known_causes(pipeline):
    return replace(pipeline, steps=[replace(step, failures=[]) for step in pipeline.steps])


def test_unknown_failure_fails_the_step_the_app_had_reached():
    tracker = track("missing_service_annotation", without_known_causes(STARTUP))

    assert statuses(tracker) == list("✓✓✓✓✓✓✗···")
    assert tracker.failure.known_cause is False
    assert tracker.failure.rule.id == "unknown"


def test_unknown_failure_after_the_app_failed_keeps_the_known_cause():
    # The log ends with Maven's BUILD FAILURE, which must not replace the cause.
    tracker = track("port_in_use")

    assert tracker.failure.known_cause
    assert tracker.failure.rule.id == "port_in_use"


# --- grouping lines into events ---


def test_application_failed_to_start_box_is_one_event():
    box = [event for event in events("missing_service_annotation") if "APPLICATION FAILED TO START" in event]

    assert len(box) == 1
    assert "LoggingFailureAnalysisReporter" in box[0].splitlines()[0]
    assert "Parameter 0 of constructor in com.demo.controller.UserController" in box[0]
    assert "Consider defining a bean of type 'com.demo.service.UserService'" in box[0]
    assert "BUILD FAILURE" not in box[0]


def test_exception_and_its_caused_by_chain_are_one_event():
    failed = [event for event in events("missing_entity_annotation") if "Application run failed" in event]

    assert len(failed) == 1
    assert failed[0].count("Caused by:") == 3
    assert "Not a managed type: class com.demo.model.User" in failed[0]
    assert "\tat com.demo.DemoAppApplication.main" in failed[0]


def test_continuation_lines_without_indent_stay_in_their_event():
    # Ambiguous mapping puts the method names on their own unindented lines.
    warning = [event for event in events("duplicate_mapping") if "cancelling refresh attempt" in event]

    assert len(warning) == 1
    assert warning[0].splitlines()[1:] == [
        "com.demo.controller.DisplayController#display()",
        "to {GET [/display]}: There is already 'displayController' bean method",
        "com.demo.controller.DisplayController#showDisplay() mapped.",
    ]


def test_every_maven_and_spring_line_starts_its_own_event():
    found = events("healthy_startup")

    assert found[0] == "[INFO] Scanning for projects..."
    assert sum(event.startswith("[INFO] BUILD SUCCESS") for event in found) == 1
    assert sum(" Started DemoAppApplication in " in event for event in found) == 1
