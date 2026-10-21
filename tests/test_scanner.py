"""Tests for the source scanner, on samples/demo-app and on small fixture projects."""

from pathlib import Path

import pytest

from fsd.scanner.spring import JavaFile, join_paths, scan

REPO = Path(__file__).parent.parent
DEMO_APP = REPO / "samples" / "demo-app"
FIXTURE = Path(__file__).parent / "fixtures" / "scanner_project"


@pytest.fixture(scope="module")
def demo():
    return scan(DEMO_APP)


@pytest.fixture(scope="module")
def fixture():
    return scan(FIXTURE)


def one_type(project, name):
    (found,) = project.types_named(name)
    return found


def mappings_of(project, controller):
    return [(m.http_method, m.path) for m in project.mappings if m.controller.name == controller]


def injections_of(project, owner):
    return [(p.where, p.type, p.name, p.line) for p in project.injection_points if p.owner.name == owner]


# --- samples/demo-app ---


def test_demo_types_with_package_file_and_line(demo):
    assert [(t.kind, t.full_name, str(t.file), t.line) for t in demo.types] == [
        ("class", "com.demo.DemoAppApplication", "src/main/java/com/demo/DemoAppApplication.java", 7),
        ("class", "com.demo.controller.DisplayController", "src/main/java/com/demo/controller/DisplayController.java", 7),
        ("class", "com.demo.controller.PostController", "src/main/java/com/demo/controller/PostController.java", 15),
        ("class", "com.demo.controller.UserController", "src/main/java/com/demo/controller/UserController.java", 18),
        ("class", "com.demo.model.Post", "src/main/java/com/demo/model/Post.java", 13),
        ("class", "com.demo.model.User", "src/main/java/com/demo/model/User.java", 15),
        ("interface", "com.demo.repository.PostRepository", "src/main/java/com/demo/repository/PostRepository.java", 7),
        ("interface", "com.demo.repository.UserRepository", "src/main/java/com/demo/repository/UserRepository.java", 7),
        ("class", "com.demo.service.PostService", "src/main/java/com/demo/service/PostService.java", 13),
        ("class", "com.demo.service.UserService", "src/main/java/com/demo/service/UserService.java", 11),
    ]


def test_demo_annotations_with_their_arguments_and_lines(demo):
    user = one_type(demo, "User")

    assert [(a.name, a.arguments, a.line) for a in user.annotations] == [
        ("Entity", {}, 13),
        ("Table", {"name": ['"users"']}, 14),
    ]
    assert [a.name for a in one_type(demo, "UserController").annotations] == ["RestController", "RequestMapping"]


def test_demo_repository_extends_jpa_repository(demo):
    assert one_type(demo, "UserRepository").extends == ["JpaRepository<User, Long>"]


def test_demo_beans(demo):
    assert [(b.name, b.type, b.reason) for b in demo.beans] == [
        ("demoAppApplication", "DemoAppApplication", "@SpringBootApplication"),
        ("displayController", "DisplayController", "@RestController"),
        ("postController", "PostController", "@RestController"),
        ("userController", "UserController", "@RestController"),
        ("postRepository", "PostRepository", "extends JpaRepository"),
        ("userRepository", "UserRepository", "extends JpaRepository"),
        ("postService", "PostService", "@Service"),
        ("userService", "UserService", "@Service"),
    ]


def test_demo_injection_points_are_worded_like_spring_errors(demo):
    # The log says: Parameter 0 of constructor in com.demo.controller.UserController
    # required a bean of type 'com.demo.service.UserService'.
    assert [(p.owner.full_name, p.where, p.type, p.name, p.line) for p in demo.injection_points] == [
        ("com.demo.controller.PostController", "Parameter 0 of constructor", "PostService", "postService", 19),
        ("com.demo.controller.UserController", "Parameter 0 of constructor", "UserService", "userService", 22),
        ("com.demo.service.PostService", "Parameter 0 of constructor", "PostRepository", "postRepository", 19),
        ("com.demo.service.PostService", "Parameter 1 of constructor", "UserRepository", "userRepository", 19),
        ("com.demo.service.UserService", "Parameter 0 of constructor", "UserRepository", "userRepository", 15),
    ]


def test_demo_application_class_and_package(demo):
    assert demo.application.name == "DemoAppApplication"
    assert demo.application.package == "com.demo"


def test_demo_request_mappings(demo):
    assert [(m.http_method, m.path, m.handler, str(m.file), m.line) for m in demo.mappings] == [
        ("GET", "/display", "DisplayController#display", "src/main/java/com/demo/controller/DisplayController.java", 9),
        ("GET", "/posts", "PostController#list", "src/main/java/com/demo/controller/PostController.java", 23),
        ("POST", "/users/{userId}/posts", "PostController#create", "src/main/java/com/demo/controller/PostController.java", 28),
        ("GET", "/users", "UserController#list", "src/main/java/com/demo/controller/UserController.java", 26),
        ("POST", "/users", "UserController#create", "src/main/java/com/demo/controller/UserController.java", 31),
        ("GET", "/users/{id}", "UserController#get", "src/main/java/com/demo/controller/UserController.java", 36),
        ("GET", "/users/search", "UserController#search", "src/main/java/com/demo/controller/UserController.java", 41),
    ]


def test_mappings_keep_the_class_and_method_paths_as_written(demo):
    assert [(m.base_path, m.own_path) for m in demo.mappings] == [
        ("", "/display"),
        ("", "/posts"),
        ("", "/users/{userId}/posts"),
        ("/users", ""),
        ("/users", ""),
        ("/users", "/{id}"),
        ("/users", "/search"),
    ]


def test_demo_parameter_annotations_are_kept(demo):
    (create,) = [m for m in one_type(demo, "UserController").methods if m.name == "create"]

    assert [(p.type, p.name, [a.name for a in p.annotations]) for p in create.parameters] == [
        ("User", "user", ["RequestBody"])
    ]


def test_scan_defaults_to_the_current_directory(monkeypatch):
    monkeypatch.chdir(DEMO_APP)

    assert scan().application.name == "DemoAppApplication"


def test_a_folder_without_java_sources_is_an_empty_project(tmp_path):
    project = scan(tmp_path)

    assert project.types == project.beans == project.mappings == []
    assert project.application is None


# --- comments and strings ---


def test_comments_and_strings_never_declare_anything(fixture):
    names = [t.name for t in fixture.types]

    for fake in ("FakeInBlockComment", "FakeInLineComment", "FakeInString", "FakeInTextBlock"):
        assert fake not in names
    assert [f.name for f in one_type(fixture, "ExampleApplication").fields] == ["NOT_A_CLASS", "OPEN_BRACE", "TEXT_BLOCK"]


def test_commented_out_annotation_does_not_count(fixture):
    assert one_type(fixture, "ReportService").annotations == []
    assert "reportService" not in [b.name for b in fixture.beans]


def test_annotation_types_are_not_listed(fixture):
    assert fixture.types_named("Audited") == []


# --- kinds and headers ---


def test_generic_class_extends_and_implements(fixture):
    box = one_type(fixture, "Box")

    assert box.extends == ["AbstractBox<T>"]
    assert box.implements == ["Serializable", "Supplier<T>"]


def test_record_components_and_interfaces(fixture):
    money = one_type(fixture, "Money")

    assert money.kind == "record"
    assert money.implements == ["Comparable<Money>"]
    assert [(f.type, f.name) for f in money.fields] == [("BigDecimal", "amount"), ("String", "currency")]
    assert [m.name for m in money.methods] == ["compareTo"]


def test_enum_constants_with_bodies_are_not_members(fixture):
    status = one_type(fixture, "Status")

    assert status.kind == "enum"
    assert status.implements == ["Supplier<String>"]
    assert [m.name for m in status.methods] == ["get"]
    assert [[p.type for p in c.parameters] for c in status.constructors] == [["String"]]


def test_nested_type_has_its_outer_class_in_its_name(fixture):
    lookups = one_type(fixture, "Lookups")

    assert lookups.enclosing == "AppConfig"
    assert lookups.full_name == "com.example.config.AppConfig.Lookups"
    assert fixture.types_named("com.example.config.AppConfig.Lookups") == [lookups]


def test_non_public_types_in_the_same_file_are_found(fixture):
    assert str(one_type(fixture, "LegacyController").file).endswith("web/ApiController.java")
    assert one_type(fixture, "Paths").line == 80


# --- beans ---


def bean(project, name):
    (found,) = [b for b in project.beans if b.name == name]
    return found


def test_bean_name_from_the_annotation_value(fixture):
    greeter = bean(fixture, "greeter")

    assert greeter.type == "EnglishGreetingService"
    assert greeter.declared_in.implements == ["GreetingService"]


def test_fully_qualified_stereotype_annotation(fixture):
    assert bean(fixture, "auditLog").reason == "@Component"


def test_record_can_be_a_bean(fixture):
    assert bean(fixture, "pricingRules").reason == "@Component"


def test_bean_methods_with_their_return_types_and_names(fixture):
    assert [(b.name, b.type, b.declared_in.name, b.line) for b in fixture.beans if b.reason == "@Bean method"] == [
        ("clock", "Clock", "AppConfig", 16),
        ("mainExecutor", "ExecutorService", "AppConfig", 21),
        ("lookup", "Map<String, List<String>>", "Lookups", 29),
    ]


def test_repositories_directly_and_through_a_base_interface(fixture):
    repositories = [(b.name, b.reason) for b in fixture.beans if b.reason.startswith("extends")]

    assert repositories == [
        ("itemRepository", "extends CrudRepository"),
        ("orderRepository", "extends JpaRepository"),
    ]


def test_no_repository_bean_base_and_plain_interfaces_are_not_beans(fixture):
    names = [b.name for b in fixture.beans]

    assert "baseRepository" not in names
    assert "greetingService" not in names


# --- injection points ---


def test_autowired_constructor_and_fields_but_not_value_or_plain_fields(fixture):
    assert injections_of(fixture, "InjectionExamples") == [
        ("Parameter 0 of constructor", "ReportService", "reportService", 32),
        ("Parameter 2 of constructor", "List<Plugin>", "plugins", 34),
        ("Field greetingService", "GreetingService", "greetingService", 14),
        ("Field auditLog", "Optional<AuditLog>", "auditLog", 16),
    ]


def test_bean_method_parameters_ask_for_beans(fixture):
    assert injections_of(fixture, "AppConfig") == [("Parameter 0 of method executor", "Clock", "clock", 21)]


def test_record_bean_components_are_constructor_injection(fixture):
    assert injections_of(fixture, "PricingRules") == [("Parameter 0 of constructor", "Clock", "clock", 10)]


def test_classes_that_are_not_beans_have_no_injection_points(fixture):
    assert injections_of(fixture, "Money") == []


# --- request mappings ---


def test_application_class(fixture):
    assert fixture.application.full_name == "com.example.ExampleApplication"


def test_value_path_arrays_methods_and_static_imports(fixture):
    assert mappings_of(fixture, "MappingExamples") == [
        ("GET", "/examples"),
        ("GET", "/examples/items/{id}"),
        ("POST", "/examples/items"),
        ("GET", "/examples/search"),
        ("PUT", "/examples/a"),
        ("PATCH", "/examples/a"),
        ("PUT", "/examples/b"),
        ("PATCH", "/examples/b"),
        (None, "/examples/any"),
        ("DELETE", "/examples/static-import"),
        ("DELETE", "/examples/items/{id:\\d+}"),
        ("GET", "/examples/multi-line"),
        ("GET", "/examples/joined/path"),
        ("GET", "/examples/<Paths.REPORTS>"),
        ("GET", "/examples/disply/"),
    ]


def test_mapping_line_is_the_annotation_line(fixture):
    lines = {m.handler: m.line for m in fixture.mappings}

    assert lines["MappingExamples#multiLine"] == 51
    assert lines["MappingExamples#typo"] == 68
    assert lines["ApiController#status"] == 20


def test_class_level_path_array(fixture):
    assert mappings_of(fixture, "ApiController") == [
        ("GET", "/api"),
        ("GET", "/v1"),
        ("POST", "/api/status/"),
        ("POST", "/v1/status/"),
    ]


def test_class_level_method_is_added_to_each_method(fixture):
    assert mappings_of(fixture, "LegacyController") == [
        ("GET", "/legacy/page"),
        ("POST", "/legacy/form"),
        ("GET", "/legacy/form"),
    ]


def test_mappings_in_comments_strings_and_interfaces_are_ignored(fixture):
    paths = [m.path for m in fixture.mappings]

    assert "/commented-out" not in " ".join(paths)
    assert "/in-a-string" not in " ".join(paths)
    assert "/remote" not in paths
    assert "MappingExamples#helper" not in [m.handler for m in fixture.mappings]


@pytest.mark.parametrize(
    ("base", "path", "joined"),
    [
        ("", "", "/"),
        ("/users", "", "/users"),
        ("", "/display", "/display"),
        ("/users", "/{id}", "/users/{id}"),
        ("users", "list", "/users/list"),
        ("/users/", "/{id}", "/users/{id}"),
        ("/", "/x", "/x"),
        ("", "/disply/", "/disply/"),
    ],
)
def test_join_paths(base, path, joined):
    assert join_paths(base, path) == joined


def test_braces_and_parens_in_strings_do_not_break_the_structure():
    source = (
        'package p;\n'
        '@RestController\n'
        'class C {\n'
        '\t@GetMapping("/a/{id}")\n'
        '\tString a() { return "}{)("; }\n'
        "\tchar b() { return '}'; }\n"
        '\t@PostMapping("/c")\n'
        '\tString c() { return "/* not a comment */"; }\n'
        '}\n'
    )
    (java_type,) = JavaFile(source, Path("C.java")).types

    assert [m.name for m in java_type.methods] == ["a", "b", "c"]
    assert [a.arguments for m in java_type.methods for a in m.annotations] == [
        {"value": ['"/a/{id}"']},
        {"value": ['"/c"']},
    ]


# --- imports, field modifiers and Lombok ---


def test_imports_are_kept_with_their_lines(demo):
    assert [(i.name, i.line) for i in one_type(demo, "User").imports] == [
        ("java.util.ArrayList", 3),
        ("java.util.List", 4),
        ("jakarta.persistence.Column", 6),
        ("jakarta.persistence.Entity", 7),
        ("jakarta.persistence.GeneratedValue", 8),
        ("jakarta.persistence.Id", 9),
        ("jakarta.persistence.OneToMany", 10),
        ("jakarta.persistence.Table", 11),
    ]


def test_static_and_wildcard_imports(fixture):
    imports = one_type(fixture, "MappingExamples").imports

    assert [(i.name, i.static) for i in imports] == [
        ("org.springframework.web.bind.annotation.RequestMethod.DELETE", True),
        ("org.springframework.web.bind.annotation.*", False),
    ]


def test_field_modifiers_and_initializers(fixture):
    fields = {f.name: (f.modifiers, f.initialized) for f in one_type(fixture, "LombokRequired").fields}

    assert fields == {
        "PREFIX": (["private", "static", "final"], True),
        "greetingService": (["private", "final"], False),
        "notFinal": (["private"], False),
        "alreadySet": (["private", "final"], True),
        "reportService": (["private"], False),
    }


def test_lombok_required_args_constructor_takes_final_and_non_null_fields(fixture):
    assert injections_of(fixture, "LombokRequired") == [
        ("Parameter 0 of constructor", "GreetingService", "greetingService", 17),
        ("Parameter 1 of constructor", "ReportService", "reportService", 24),
    ]


def test_lombok_all_args_constructor_takes_every_field_that_can_be_set(fixture):
    assert injections_of(fixture, "LombokAll") == [
        ("Parameter 0 of constructor", "GreetingService", "greetingService", 13),
        ("Parameter 1 of constructor", "AuditLog", "auditLog", 17),
    ]


def test_lombok_data_adds_no_constructor_when_there_is_one():
    source = "package p;\n@lombok.Data\n@Service\nclass C {\n\tprivate final A a;\n\tC() { a = null; }\n}\n"
    (java_type,) = JavaFile(source, Path("C.java")).types

    assert [[p.name for p in c.parameters] for c in java_type.constructors] == [[]]


# --- application properties ---


def test_properties_file_keys_values_and_lines(fixture):
    from_properties = [(p.key, p.value, p.line) for p in fixture.properties if p.file.suffix == ".properties"]

    assert from_properties == [
        ("spring.application.name", "example", 2),
        ("server.port", "9090", 3),
        ("app.greeting", "Hello world", 4),
        ("app.path", "C:\\temp", 7),
    ]


def test_yaml_keys_are_flattened_with_their_lines(fixture):
    from_yaml = [(p.key, p.value, p.line) for p in fixture.properties if p.file.suffix == ".yml"]

    assert from_yaml == [
        ("server.port", "7070", 2),
        ("spring.datasource.url", "jdbc:postgresql://db.example.com:5432/shop", 5),
        ("spring.datasource.username", "shop", 6),
        ("spring.profiles.include[0]", "local", 9),
        ("spring.profiles.include[1]", "debug", 10),
    ]


def test_properties_file_wins_over_yaml(fixture):
    port = fixture.property("server.port")

    assert (port.value, str(port.file)) == ("9090", "src/main/resources/application.properties")
    assert fixture.property("spring.datasource.url").value == "jdbc:postgresql://db.example.com:5432/shop"
    assert fixture.property("no.such.key") is None


def test_demo_properties(demo):
    assert [(p.key, p.value, str(p.file), p.line) for p in demo.properties] == [
        ("spring.application.name", "demo-app", "src/main/resources/application.properties", 1)
    ]


def test_broken_yaml_is_skipped(tmp_path):
    resources = tmp_path / "src" / "main" / "resources"
    resources.mkdir(parents=True)
    (resources / "application.yml").write_text("server:\n  port: [8080\n")

    assert scan(tmp_path).properties == []
