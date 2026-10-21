"""Startup and request checklists by name, and running them against the project's source.

A checklist runs its checks in order and stops at the first one that fails:
that one says what is wrong and how to fix it. The startup checks are in
startup.py, the request checks in request.py, what they share in common.py.
"""

from pathlib import Path

from fsd.checks.common import Check, CheckResult
from fsd.checks.request import (
    body_is_read_by_handler,
    body_is_valid_json,
    closest_mapping,
    column_has_field,
    json_field_has_right_type,
    json_fields_do_not_loop,
    lazy_relationship_is_loaded,
    mapping_is_registered,
    parameter_has_right_type,
    path_accepts_method,
    path_differs_by_synonym,
    path_differs_by_trailing_slash,
    path_has_other_method,
    request_has_body,
    request_param_is_present,
    table_exists,
    table_has_entity,
    unique_column_has_field,
)
from fsd.checks.startup import (
    Listener,
    bean_type_exists,
    bean_type_is_annotated,
    bean_type_is_scanned,
    class_has_entity_annotation,
    database_is_running,
    datasource_url_is_set,
    entity_has_id,
    entity_is_scanned,
    entity_uses_jakarta_persistence,
    interface_has_bean_implementation,
    listening_process,
    mapping_is_unique,
    port_is_used_by,
    property_has_valid_value,
    source_compiles,
)
from fsd.scanner.spring import SpringProject, scan
from fsd.tracker import Failure, Trace

__all__ = ["CHECKLISTS", "CheckResult", "Listener", "diagnose", "diagnose_request", "listening_process", "run_checklist"]


def port_is_free(project: SpringProject, details: dict[str, str]) -> CheckResult:
    """Ask the OS which program holds the port; kept with the checklists so a test can stand in for the OS here."""
    port = int(details["port"])
    return port_is_used_by(project, port, listening_process(port))


CHECKLISTS: dict[str, list[Check]] = {
    "missing_bean": [bean_type_exists, bean_type_is_annotated, bean_type_is_scanned, interface_has_bean_implementation],
    "not_a_managed_type": [class_has_entity_annotation, entity_uses_jakarta_persistence, entity_has_id, entity_is_scanned],
    "ambiguous_mapping": [mapping_is_unique],
    "port_in_use": [port_is_free],
    "compile_error": [source_compiles],
    "property_bind_failed": [property_has_valid_value],
    "connection_refused": [datasource_url_is_set, database_is_running],
    "no_mapping": [
        mapping_is_registered,
        path_has_other_method,
        path_differs_by_trailing_slash,
        path_differs_by_synonym,
        closest_mapping,
    ],
    "method_not_allowed": [path_accepts_method],
    "missing_body": [request_has_body],
    "malformed_json": [body_is_read_by_handler, body_is_valid_json],
    "json_wrong_type": [body_is_read_by_handler, json_field_has_right_type],
    "argument_type_mismatch": [parameter_has_right_type],
    "missing_request_param": [request_param_is_present],
    "missing_column": [table_has_entity, column_has_field],
    "missing_table": [table_exists],
    "duplicate_key": [unique_column_has_field],
    "lazy_initialization": [lazy_relationship_is_loaded],
    "json_recursion": [json_fields_do_not_loop],
}


def run_checklist(name: str, project: SpringProject, details: dict[str, str]) -> list[CheckResult]:
    """Run checklist `name` in order, up to and including the first check that fails."""
    results = []
    for check in CHECKLISTS[name]:
        result = check(project, details)
        if result is None:
            continue
        results.append(result)
        if not result.passed:
            break
    return results


def diagnose(failure: Failure, root: Path | str = ".") -> list[CheckResult]:
    """Run the failure's checklist against the project in `root`."""
    if not failure.rule.checklist:
        return []
    return run_checklist(failure.rule.checklist, scan(root), failure.details)


def request_details(trace: Trace) -> dict[str, str]:
    """What the log told about a failed request: each step's details, the failure's, and the request itself."""
    details: dict[str, str] = {}
    for step in trace.run.pipeline.steps:
        details.update(trace.run.details[step.id])
    details.update(trace.run.failure.details)
    return {**details, "http_method": trace.method, "request_path": trace.path}


def diagnose_request(trace: Trace, root: Path | str = ".") -> list[CheckResult]:
    """Run the checklist of the request's failure against the project in `root`."""
    failure = trace.run.failure
    if failure is None or not failure.rule.checklist:
        return []
    return run_checklist(failure.rule.checklist, scan(root), request_details(trace))
