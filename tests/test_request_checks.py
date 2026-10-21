"""Tests for the request checklists, and the path comparisons they use.

Each checklist runs on the trace the tracker builds from a real log in
tests/logs/, against the source it came from: a copy of samples/demo-app,
broken with the same edit as scripts/capture_logs.py made when there was one.
A few tests break the copy another way, to reach a check the same log leads to
when the source is different.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

from fsd.checks.paths import (
    differences,
    edit_distance,
    filled,
    matches,
    synonym_variants,
    wanted,
    with_query_value,
    with_variable,
)
from fsd.checks.spring import CHECKLISTS, diagnose_request, request_details
from fsd.tracker import RequestTracker, load_pipeline

REPO = Path(__file__).parent.parent
LOGS = Path(__file__).parent / "logs"
DEMO_APP = REPO / "samples" / "demo-app"
REQUEST = load_pipeline("spring_request")
CONTROLLERS = Path("src/main/java/com/demo/controller")
NO_SYNONYM = "only by a synonym (all/everything, find/get, remove/delete, user/users)"


def load_capture_cases():
    spec = importlib.util.spec_from_file_location("capture_logs", REPO / "scripts" / "capture_logs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {case.name: case for case in module.CASES}, module.APP


CASES, CAPTURED_APP = load_capture_cases()


def logged_trace(log: str):
    found = []
    tracker = RequestTracker(REQUEST, found.append)
    for line in (LOGS / f"{log}.log").open(encoding="utf-8"):
        tracker.feed(line)
    tracker.finish()
    return found[-1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8")


def demo_copy(tmp_path: Path, case: str | None = None) -> Path:
    """A copy of the demo app's source, with the same break as when tests/logs/<case>.log was captured."""
    copy = tmp_path / "demo-app"
    shutil.copytree(DEMO_APP / "src", copy / "src")
    for edit in CASES[case].edits if case else []:
        edit.apply_to(copy / edit.path.relative_to(CAPTURED_APP))
    return copy


def outcomes(results) -> list[tuple[bool, str]]:
    return [(result.passed, result.message) for result in results]


def test_every_request_failure_with_a_checklist_names_one_that_exists():
    rules = [rule for step in REQUEST.steps for rule in step.failures if rule.checklist]

    assert {rule.id for rule in rules} == {
        "no_handler", "method_not_allowed", "missing_body", "json_wrong_type", "malformed_json",
        "argument_type_mismatch", "missing_request_param", "missing_column", "missing_table", "duplicate_key",
        "lazy_initialization", "json_recursion",
    }  # fmt: skip
    for rule in rules:
        assert rule.checklist in CHECKLISTS, rule.id


def test_the_checklist_gets_the_request_and_the_handler_it_was_mapped_to():
    details = request_details(logged_trace("query_param_type_400"))

    assert details["http_method"] == "GET"
    assert details["request_path"] == "/users/search?name=Ada&limit=ten"
    assert (details["controller"], details["handler"]) == ("com.demo.controller.UserController", "search")
    assert (details["parameter"], details["value"]) == ("limit", "ten")


def test_requests_that_did_not_fail_on_a_known_cause_have_no_checks():
    assert diagnose_request(logged_trace("healthy_get_users"), DEMO_APP) == []
    assert diagnose_request(logged_trace("service_exception_500"), DEMO_APP) == []


# --- 404 ---


def test_404_for_a_typo_names_the_closest_mapping(tmp_path):
    results = diagnose_request(logged_trace("display_typo_404"), demo_copy(tmp_path, "display_typo_404"))

    assert outcomes(results) == [
        (True, "no mapping has the path /display with another HTTP method"),
        (True, "no mapping differs from /display only by the / at the end"),
        (True, f"no mapping differs from /display {NO_SYNONYM}"),
        (False, 'the closest of your 7 mappings is GET /disply/: "a" missing, extra "/" at the end'),
    ]
    assert results[-1].location == "src/main/java/com/demo/controller/DisplayController.java:9"
    assert results[-1].fix == (
        'change @GetMapping("/disply/") to @GetMapping("/display") (DisplayController.java:9), or call /disply/ instead'
    )
    assert results[-1].note == (
        'since Spring Boot 3, "/display/" and "/display" are different URLs (Spring Boot 2 treated them as one)'
    )


def test_404_for_a_trailing_slash_explains_spring_boot_3(tmp_path):
    results = diagnose_request(logged_trace("trailing_slash_404"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (True, "no mapping has the path /users/ with another HTTP method"),
        (False, "GET /users is mapped, and the request was for /users/: the / at the end makes it another URL"),
    ]
    assert results[-1].location == "src/main/java/com/demo/controller/UserController.java:26"
    assert results[-1].fix == 'call /users, or map both: change @GetMapping to @GetMapping({"", "/"}) (UserController.java:26)'
    assert results[-1].note.startswith('since Spring Boot 3, "/users/" and "/users" are different URLs')


def test_404_for_a_synonym_matches_path_variables_as_wildcards(tmp_path):
    results = diagnose_request(logged_trace("singular_path_404"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (True, "no mapping has the path /user/1 with another HTTP method"),
        (True, "no mapping differs from /user/1 only by the / at the end"),
        (False, 'GET /users/{id} is mapped: it says "users" where the request has "user"'),
    ]
    assert results[-1].location == "src/main/java/com/demo/controller/UserController.java:36"
    assert results[-1].fix == (
        'call /users/1, or change @RequestMapping("/users") to @RequestMapping("/user") '
        "(UserController.java:17; every path in UserController starts with it)"
    )


def test_404_for_a_path_mapped_with_another_method_says_which_to_use(tmp_path):
    # Spring itself answers 405 here; a 404 means the app that answered is older than the source.
    project = demo_copy(tmp_path)
    replace_once(project / CONTROLLERS / "DisplayController.java", '@GetMapping("/display")', '@PostMapping("/display")')

    results = diagnose_request(logged_trace("display_typo_404"), project)

    assert outcomes(results) == [(False, "/display is mapped for POST, not GET")]
    assert results[0].fix == (
        "send POST /display (a browser's address bar can only send GET), "
        'or change @PostMapping("/display") to @GetMapping("/display") (DisplayController.java:9)'
    )


def test_404_for_a_path_in_a_class_without_rest_controller(tmp_path):
    project = demo_copy(tmp_path)
    replace_once(project / CONTROLLERS / "DisplayController.java", "@RestController\npublic", "public")

    results = diagnose_request(logged_trace("display_typo_404"), project)

    assert outcomes(results) == [
        (False, "DisplayController maps GET /display but has no @RestController, so Spring never serves it")
    ]
    assert results[0].fix == 'add @RestController on the line above "public class DisplayController" (DisplayController.java:6)'


def test_404_for_a_path_the_source_has_means_an_old_app():
    results = diagnose_request(logged_trace("display_typo_404"), DEMO_APP)

    assert outcomes(results) == [(False, "GET /display is in your source, but the app that answered does not have it")]
    assert results[0].fix == "restart the app: it is running older code than your source"


def test_404_with_nothing_close_lists_every_mapping(tmp_path):
    project = demo_copy(tmp_path)
    replace_once(project / CONTROLLERS / "DisplayController.java", '@GetMapping("/display")', '@GetMapping("/hello")')

    results = diagnose_request(logged_trace("display_typo_404"), project)

    assert outcomes(results)[-1] == (
        False,
        "none of your 7 mappings is close to /display:\n"
        "GET /hello                  DisplayController#display()  DisplayController.java:9\n"
        "GET /posts                  PostController#list()        PostController.java:23\n"
        "POST /users/{userId}/posts  PostController#create()      PostController.java:28\n"
        "GET /users                  UserController#list()        UserController.java:26\n"
        "POST /users                 UserController#create()      UserController.java:31\n"
        "GET /users/{id}             UserController#get()         UserController.java:36\n"
        "GET /users/search           UserController#search()      UserController.java:41",
    )
    assert results[-1].fix == 'call one of them, or add a method with @GetMapping("/display") to a @RestController class'


def test_404_in_a_project_without_mappings(tmp_path):
    results = diagnose_request(logged_trace("display_typo_404"), tmp_path)

    assert outcomes(results)[-1] == (
        False,
        "your source has no request mappings: no @GetMapping, @PostMapping or @RequestMapping",
    )


# --- 405 ---


def test_405_lists_the_methods_the_path_accepts(tmp_path):
    results = diagnose_request(logged_trace("wrong_method_405"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (False, "/users/1 accepts GET, not DELETE:\nGET /users/{id}  UserController#get()  UserController.java:36")
    ]
    assert results[0].location == "src/main/java/com/demo/controller/UserController.java:36"
    assert results[0].fix == 'send GET /users/1, or add a method with @DeleteMapping("/{id}") to UserController for DELETE'


# --- 400 ---


def test_400_missing_body_shows_the_request_body_parameter(tmp_path):
    results = diagnose_request(logged_trace("post_missing_body"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (False, "UserController.create() reads the body into @RequestBody User user, and the request had none")
    ]
    assert results[0].location == "src/main/java/com/demo/controller/UserController.java:32"
    assert results[0].fix == (
        "send a JSON body with Content-Type: application/json, like: "
        """fsd try POST /users --body '{"name": "text"}'. If the body may be left out, use @RequestBody(required = false)"""
    )


def test_400_malformed_json(tmp_path):
    results = diagnose_request(logged_trace("post_malformed_json"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (True, "UserController.create() reads the JSON body into User user"),
        (False, "the body is not valid JSON: Unexpected end-of-input: expected close marker for Object"),
    ]
    assert results[-1].fix.endswith('names and text need double quotes, like {"name": "text"}')


def test_400_json_field_of_the_wrong_type_finds_the_field_by_its_type(tmp_path):
    results = diagnose_request(logged_trace("post_wrong_field_type"), demo_copy(tmp_path))

    assert outcomes(results)[-1] == (
        False,
        'the body has "abc", which is not a valid Long; the only Long field of User is id',
    )
    assert results[-1].location == "src/main/java/com/demo/model/User.java:19"
    assert results[-1].fix == 'send a whole number for "id", like "id": 10'


def test_400_json_wrong_type_with_several_fields_of_that_type(tmp_path):
    project = demo_copy(tmp_path)
    replace_once(project / "src/main/java/com/demo/model/User.java", "private String name;", "private String name;\n\n\tprivate Long age;")

    results = diagnose_request(logged_trace("post_wrong_field_type"), project)

    assert outcomes(results)[-1] == (False, 'the body has "abc", which is not a valid Long; the Long fields of User are id and age')
    assert results[-1].fix == 'send a whole number for "id" or "age"'


def test_400_path_variable_of_the_wrong_type(tmp_path):
    results = diagnose_request(logged_trace("path_variable_type_400"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (False, '{id} in /users/{id} is read into Long id, so it must be a whole number; the request has "abc"')
    ]
    assert results[0].location == "src/main/java/com/demo/controller/UserController.java:37"
    assert results[0].fix == "call /users/10, or, if any text is allowed there, change Long id to String id"


def test_400_query_value_of_the_wrong_type_keeps_the_other_parameters(tmp_path):
    results = diagnose_request(logged_trace("query_param_type_400"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (False, '?limit= is read into int limit, so it must be a whole number; the request has "ten"')
    ]
    assert results[0].location == "src/main/java/com/demo/controller/UserController.java:42"
    assert results[0].fix == "call /users/search?name=Ada&limit=10, or, if any text is allowed there, change int limit to String limit"


def test_400_missing_request_param(tmp_path):
    results = diagnose_request(logged_trace("missing_request_param_400"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (False, "UserController.search() needs ?name= for @RequestParam String name, and the request had none")
    ]
    assert results[0].location == "src/main/java/com/demo/controller/UserController.java:42"
    assert results[0].fix == (
        "call /users/search?name=text, or make it optional: @RequestParam(required = false) String name, "
        'or give it a default: @RequestParam(defaultValue = "text") String name'
    )


def test_400_missing_request_param_bound_by_another_name(tmp_path):
    project = demo_copy(tmp_path)
    replace_once(project / CONTROLLERS / "UserController.java", "@RequestParam String name", '@RequestParam("name") String query')

    (result,) = diagnose_request(logged_trace("missing_request_param_400"), project)

    assert result.message == 'UserController.search() needs ?name= for @RequestParam String query, and the request had none'
    assert '@RequestParam(name = "name", required = false) String query' in result.fix


# --- SQL errors ---

DDL_AUTO_NOTE = (
    "spring.jpa.hibernate.ddl-auto=none (src/main/resources/application.properties:2) leaves the schema to you; "
    "with update, Hibernate adds the tables and columns that are missing"
)


def test_missing_column_shows_the_entity_field_mapped_to_it(tmp_path):
    results = diagnose_request(logged_trace("missing_column_sql"), demo_copy(tmp_path, "missing_column_sql"))

    assert outcomes(results) == [
        (True, "table users belongs to entity User"),
        (False, "User.email is stored in column email, and table users has no such column"),
    ]
    assert [r.location for r in results] == ["src/main/java/com/demo/model/User.java:15", "src/main/java/com/demo/model/User.java:24"]
    assert results[-1].fix == (
        'add email varchar(255) to "create table users" in schema.sql:1; '
        "or, if email is not meant to be stored, mark it @Transient"
    )
    assert results[-1].note == DDL_AUTO_NOTE


def test_missing_column_without_a_schema_script(tmp_path):
    project = demo_copy(tmp_path)
    replace_once(project / "src/main/java/com/demo/model/User.java", "\tprivate String name;\n", "\tprivate String name;\n\n\tprivate String email;\n")

    results = diagnose_request(logged_trace("missing_column_sql"), project)

    assert results[-1].fix.startswith("add the column to the database: alter table users add column email varchar(255);")
    assert results[-1].note == ""


def test_missing_table_shows_the_entity_and_the_table_to_create(tmp_path):
    results = diagnose_request(logged_trace("missing_table_sql"), demo_copy(tmp_path, "missing_table_sql"))

    assert outcomes(results) == [(False, "Post is stored in table posts, and the database has no such table")]
    assert results[0].location == "src/main/java/com/demo/model/Post.java:12"
    assert results[0].fix == (
        "add it to schema.sql: create table posts (id bigint primary key, title varchar(255), user_id bigint); "
        "create sequence posts_seq start with 1 increment by 50;"
    )
    assert results[0].note == DDL_AUTO_NOTE


def test_duplicate_key_shows_the_unique_column(tmp_path):
    results = diagnose_request(logged_trace("duplicate_key_sql"), demo_copy(tmp_path))

    assert outcomes(results) == [
        (False, "User.name must be unique (@Column(unique = true)), and a row with name 'Ada' already exists")
    ]
    assert results[0].location == "src/main/java/com/demo/model/User.java:22"
    assert results[0].fix == (
        "send a name that is not taken yet. To answer 409 Conflict instead of 500, check first with "
        "UserRepository.existsByName(name), or handle DataIntegrityViolationException in an @ExceptionHandler"
    )


def test_duplicate_key_on_a_column_made_unique_only_in_the_database(tmp_path):
    project = demo_copy(tmp_path)
    replace_once(project / "src/main/java/com/demo/model/User.java", "\t@Column(unique = true)\n", "")

    (result,) = diagnose_request(logged_trace("duplicate_key_sql"), project)

    assert result.message == "User.name must be unique (a unique index in the database), and a row with name 'Ada' already exists"


def test_lazy_loading_shows_the_relationship_and_the_usual_fixes(tmp_path):
    results = diagnose_request(logged_trace("lazy_initialization"), demo_copy(tmp_path, "lazy_initialization"))

    assert outcomes(results) == [
        (
            False,
            "User.posts is a lazy @OneToMany, and it was read while the response was written as JSON, "
            "after the database session had closed",
        )
    ]
    assert results[0].location == "src/main/java/com/demo/model/User.java:25"
    assert results[0].fix == (
        'load it with the query: @EntityGraph(attributePaths = "posts") on the UserRepository method, '
        'or "join fetch" in a @Query; or return a DTO with only what the response needs; '
        "or, if the response does not need posts, add @JsonIgnore to it"
    )
    assert results[0].note.startswith("spring.jpa.open-in-view=false (src/main/resources/application.properties:2)")


# --- the response ---


def test_json_recursion_shows_the_two_fields_that_point_at_each_other(tmp_path):
    results = diagnose_request(logged_trace("json_recursion"), demo_copy(tmp_path, "json_recursion"))

    assert outcomes(results) == [
        (
            False,
            "User.posts and Post.user point at each other, so the JSON goes "
            "User → posts → Post → user → User → ... until Jackson stops:\n"
            'User.posts  @OneToMany(mappedBy = "user")  User.java:25\n'
            "Post.user   @ManyToOne                     Post.java:22",
        )
    ]
    assert results[0].location == "src/main/java/com/demo/model/Post.java:22"
    assert results[0].fix == (
        "break the loop on one side: add @JsonIgnore to Post.user (Post.java:22), or put @JsonManagedReference "
        "on User.posts and @JsonBackReference on Post.user, or return a DTO that leaves out the link back"
    )
    assert "com.fasterxml.jackson.annotation.JsonIgnore" in results[0].note


def test_json_recursion_starts_from_what_the_handler_returns(tmp_path):
    traces = []
    tracker = RequestTracker(REQUEST, traces.append)
    for line in (LOGS / "json_recursion.log").open(encoding="utf-8"):
        tracker.feed(line)
    (create_post,) = [t for t in traces if t.path == "/users/1/posts"]

    (result,) = diagnose_request(create_post, demo_copy(tmp_path, "json_recursion"))

    assert result.message.startswith("Post.user and User.posts point at each other, so the JSON goes Post → user → User")


def test_json_recursion_once_fixed_finds_no_loop():
    # The demo app as it is: @JsonIgnore on Post.user.
    (result,) = diagnose_request(logged_trace("json_recursion"), DEMO_APP)

    assert result.message == "no two entity fields point at each other: some object in the response contains itself"


# --- comparing paths ---


@pytest.mark.parametrize(
    ("template", "path", "expected"),
    [
        ("/users/{id}", "/users/5", True),
        ("/users/{id}", "/users/", False),
        ("/users/{id}", "/users/5/", False),
        ("/users", "/users/", False),
        ("/items/{id:\\d+}", "/items/12", True),
        ("/items/{id:\\d+}", "/items/abc", False),
        ("/items/{id:\\p{Alpha}+}", "/items/abc", True),  # Java regex Python cannot read: any segment
        ("/files/**", "/files", True),
        ("/files/**", "/files/a/b", True),
        ("/files/{*rest}", "/files/a/b", True),
        ("/docs/*.html", "/docs/a.html", True),
        ("/docs/*.html", "/docs/a/b.html", False),
    ],
)
def test_paths_match_like_spring(template, path, expected):
    assert matches(template, path) is expected


def test_path_variables_are_filled_from_the_request_or_kept():
    assert filled("/users/{id}", "/user/1") == "/users/1"
    assert filled("/users/{id}", "/users") == "/users/{id}"
    assert wanted("/users/{id}", "/user/1") == "/user/{id}"
    assert with_variable("/users/{id}", "/users/abc", "id", "10") == "/users/10"
    assert with_query_value("/a", "name", "text") == "/a?name=text"


def test_edit_distance_and_the_differences_it_names():
    assert edit_distance("/display", "/disply/") == 2
    assert edit_distance("", "abc") == 3
    assert differences("/display", "/disply/") == ['"a" missing', 'extra "/" at the end']
    assert differences("/user/1", "/users/1") == ['extra "s"']
    assert differences("/users/", "/users") == ['no "/" at the end']
    assert differences("/shop", "/ship") == ['"i" instead of "o"']


def test_synonyms_are_swapped_in_the_same_case():
    assert synonym_variants("/findAll") == [
        ("/findEverything", [("All", "Everything")]),
        ("/getAll", [("find", "get")]),
        ("/getEverything", [("find", "get"), ("All", "Everything")]),
    ]
    assert synonym_variants("/username") == []
