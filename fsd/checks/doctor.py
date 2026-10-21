"""fsd doctor: the checks that need only the source, run over the whole project before the app starts.

Each check looks at every bean, mapping or entity at once and returns one
CheckResult per problem it finds, or a single passed one when there is none.
The wording comes from the startup and request checks, so a problem reads the
same here as when fsd run finds it in the log.
"""

import os
import re
import socket
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from fsd.checks.common import CheckResult, find_type, is_under, package_is_scanned
from fsd.checks.paths import edit_distance, has_wildcard, segments, with_other_slash
from fsd.checks.request import change_path_fix, known_mappings, mapping_rows, slash_note
from fsd.checks.spring import port_is_free, run_checklist
from fsd.checks.startup import (
    LIBRARY_BEAN_FIXES,
    bean_type_exists,
    class_has_entity_annotation,
    entity_has_id,
    entity_uses_jakarta_persistence,
    mapping_is_unique,
    property_has_valid_value,
)
from fsd.scanner.build import build_file, read_dependencies
from fsd.scanner.properties import RESOURCES, Property, properties_file, yaml_file
from fsd.scanner.spring import (
    SPRING_DATA_REPOSITORIES,
    Bean,
    InjectionPoint,
    JavaType,
    RequestMapping,
    SpringProject,
    annotation_named,
    java_string,
    scan,
    simple_type_name,
)

# Types that hold the beans asked for rather than being one: List<Plugin>, Optional<Mailer>, ...
BEAN_HOLDERS = {"List", "Set", "Collection", "Iterable", "Map", "Optional", "ObjectProvider", "ObjectFactory", "Provider", "Stream"}
# Mapping attributes that let two methods share a path and HTTP method.
MAPPING_CONDITIONS = ("params", "headers", "consumes", "produces")
MAX_TYPO_LETTERS = 2
VARIABLE = re.compile(r"\{[^/]*\}")
JPA_STARTERS = {"spring-boot-starter-data-jpa", "spring-data-jpa"}
EMBEDDED_DATABASES = {"h2": "H2", "hsqldb": "HSQLDB", "derby": "Derby"}
DOCKER_DATABASES = {"spring-boot-docker-compose", "spring-boot-testcontainers"}
H2_DEPENDENCY = {
    "pom.xml": "<dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency>",
    "build.gradle": "runtimeOnly 'com.h2database:h2'",
    "build.gradle.kts": 'runtimeOnly("com.h2database:h2")',
}
DEFAULT_PORT = 8080
PLACEHOLDER_DEFAULT = re.compile(r"\$\{[^:}]+:(?P<default>[^}]*)\}")
CONNECT_SECONDS = 0.5


@dataclass
class Section:
    title: str
    results: list[CheckResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # checks that crashed: a bug in fsd, not in the project


# --- where Spring Boot looks ---


def named_packages(app: JavaType, annotation_name: str, *attributes: str) -> list[str]:
    annotation = annotation_named(app.annotations, annotation_name)
    values = annotation.values(*attributes) if annotation else []
    return [text for value in values if (text := java_string(value))]


def bean_packages(app: JavaType) -> list[str]:
    """The packages Spring Boot looks for beans in: scanBasePackages or @ComponentScan if set, else the app's own."""
    named = named_packages(app, "SpringBootApplication", "scanBasePackages")
    return named or named_packages(app, "ComponentScan", "value", "basePackages") or [app.package]


def repository_packages(app: JavaType) -> list[str]:
    return named_packages(app, "EnableJpaRepositories", "value", "basePackages") or [app.package]


def entity_packages(app: JavaType) -> list[str]:
    return named_packages(app, "EntityScan", "value", "basePackages") or [app.package]


def is_scanned(java_type: JavaType, packages: list[str]) -> bool:
    return any(is_under(java_type.package, package) for package in packages)


def outside_packages(java_type: JavaType, app: JavaType, packages: list[str], what: str, how: str) -> CheckResult:
    """The failed check for a class Spring Boot never finds, worded like package_is_scanned."""
    if packages == [app.package]:
        return package_is_scanned(java_type, app, what, how)
    return CheckResult(
        False,
        f"{java_type.name} is in {java_type.package}, outside {', '.join(packages)} where Spring Boot looks for {what}",
        f"move {java_type.name} into one of those packages, or {how}",
        java_type.file,
        java_type.line,
    )


def counted(count: int, word: str, words: str = "") -> str:
    """1 bean, 8 beans, 2 entities."""
    return f"{count} {word}" if count == 1 else f"{count} {words or word + 's'}"


def names_with_and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


# --- beans ---


def no_application_class() -> CheckResult:
    return CheckResult(
        False,
        "no class in src/main/java has @SpringBootApplication",
        "run fsd doctor in the app's own folder (in a multi-module build, the module with the main class), "
        "or add @SpringBootApplication to the class with the main method",
    )


def type_beans(project: SpringProject) -> list[Bean]:
    """The beans Spring Boot finds by scanning: classes with an annotation, and repositories."""
    return [b for b in project.beans if b.reason != "@Bean method"]


def beans_are_scanned(project: SpringProject) -> list[CheckResult]:
    app = project.application
    if app is None:
        return [no_application_class()]
    failed = []
    for bean in type_beans(project):
        java_type = bean.declared_in
        if bean.reason.startswith("extends"):
            packages, what = repository_packages(app), "repositories"
            how = f'add @EnableJpaRepositories("{java_type.package}") to {app.name}'
        else:
            packages, what = bean_packages(app), "beans"
            how = f'add scanBasePackages = "{java_type.package}" to @SpringBootApplication in {app.name}'
        if not is_scanned(java_type, packages):
            failed.append(outside_packages(java_type, app, packages, what, how))
    if failed:
        return failed
    where = " and ".join(sorted(set(bean_packages(app) + repository_packages(app))))
    return [CheckResult(True, f"every bean is in {where} or a package below it ({counted(len(type_beans(project)), 'bean')})")]


def wanted_type(point: InjectionPoint) -> str:
    """The type an injection point asks for, by its full name when its file imports it."""
    name = simple_type_name(point.type)
    if "." in point.type.split("<")[0]:
        return point.type.split("<")[0].strip()
    imported = next((i.name for i in point.owner.imports if i.name.endswith(f".{name}") and not i.static), None)
    return imported or name


def is_subtype(project: SpringProject, java_type: JavaType, name: str, seen: set[str] | None = None) -> bool:
    """Whether `java_type` implements or extends the type `name`, directly or through its supertypes."""
    seen = seen if seen is not None else set()
    seen.add(java_type.full_name)
    for parent in java_type.extends + java_type.implements:
        parent_name = simple_type_name(parent)
        if parent_name == name:
            return True
        parent_type = find_type(project, parent_name)
        if parent_type and parent_type.full_name not in seen and is_subtype(project, parent_type, name, seen):
            return True
    return False


def has_bean(project: SpringProject, type_name: str) -> bool:
    """A bean of `type_name`: the class itself, a class that implements it, or a @Bean method that returns it."""
    name = simple_type_name(type_name)
    for bean in project.beans:
        if simple_type_name(bean.type) == name:
            return True
        if bean.reason != "@Bean method" and is_subtype(project, bean.declared_in, name):
            return True
    return False


def missing_bean_cause(project: SpringProject, type_name: str) -> CheckResult | None:
    """Why there is no bean of `type_name`, from the missing-bean checklist; None if it can't be told from the source."""
    if find_type(project, type_name) is None:
        if simple_type_name(type_name) not in LIBRARY_BEAN_FIXES:
            return None  # a library type: Spring Boot's auto-configuration may well provide it
        return bean_type_exists(project, {"bean_type": type_name})
    results = run_checklist("missing_bean", project, {"bean_type": type_name})
    return next((r for r in results if not r.passed), None)


def is_created(point: InjectionPoint, app: JavaType | None) -> bool:
    """Whether Spring creates the class that asks: one outside the scanned packages is reported as such instead."""
    return app is None or is_scanned(point.owner, bean_packages(app))


def injections_have_beans(project: SpringProject) -> list[CheckResult]:
    app = project.application
    asked: dict[str, list[InjectionPoint]] = {}
    for point in project.injection_points:
        if is_created(point, app) and simple_type_name(point.type) not in BEAN_HOLDERS:
            asked.setdefault(wanted_type(point), []).append(point)
    failed = []
    for type_name, points in asked.items():
        cause = None if has_bean(project, type_name) else missing_bean_cause(project, type_name)
        if cause is not None:
            owners = list(dict.fromkeys(p.owner.name for p in points))
            asks = "asks" if len(owners) == 1 else "ask"
            failed.append(replace(cause, message=f"{names_with_and(owners)} {asks} for {simple_type_name(type_name)}: {cause.message}"))
    if failed:
        return failed
    count = sum(len(points) for points in asked.values())
    return [CheckResult(True, f"every bean your classes ask for exists ({count} asked for)")] if count else []


# --- mappings ---


def handler_name(mapping: RequestMapping) -> str:
    """com.demo.DisplayController#display(), as Spring's Ambiguous mapping error names it."""
    return f"{mapping.controller.full_name}#{mapping.method}()"


def conditions(mapping: RequestMapping) -> tuple:
    """The params, headers, consumes and produces of the mapping's annotation, which make it narrower."""
    method = next((m for m in mapping.controller.methods if m.name == mapping.method), None)
    annotation = next((a for a in method.annotations if a.line == mapping.line), None) if method else None
    if annotation is None:
        return ()
    return tuple((name, tuple(annotation.values(name))) for name in MAPPING_CONDITIONS if annotation.values(name))


def path_shape(path: str) -> str:
    """The path with its variables blanked: /users/{id} and /users/{userId} match the same requests."""
    return VARIABLE.sub("{}", path)


def mappings_are_unique(project: SpringProject) -> list[CheckResult]:
    mappings = known_mappings(project)
    groups: dict[tuple, list[RequestMapping]] = {}
    for mapping in mappings:
        groups.setdefault((mapping.http_method, path_shape(mapping.path), conditions(mapping)), []).append(mapping)
    failed = []
    for group in groups.values():
        handlers = list({(m.controller.full_name, m.method): m for m in group}.values())
        if len(handlers) > 1:
            first, second = handlers[:2]
            details = {"method": handler_name(first), "existing_method": handler_name(second), "path": first.path}
            failed.append(mapping_is_unique(project, {**details, "http_method": first.http_method or ""}))
    if failed:
        return failed
    if not mappings:
        return []
    return [CheckResult(True, f"no two mappings have the same path and HTTP method ({counted(len(mappings), 'mapping')})")]


def typo_segment(path: str, other: str) -> tuple[int, int] | None:
    """Where two paths differ by a typo in one segment, and by how many letters; None if they don't.

    /user and /users do; /v1 and /v2, /follow and /unfollow, and a segment that is a variable don't.
    """
    parts, other_parts = segments(path), segments(other)
    if len(parts) != len(other_parts):
        return None
    differing = [i for i, (a, b) in enumerate(zip(parts, other_parts)) if a != b]
    if len(differing) != 1:
        return None
    index = differing[0]
    a, b = parts[index], other_parts[index]
    shorter, longer = sorted((a, b), key=len)
    if has_wildcard(a) or has_wildcard(b) or re.sub(r"\d", "", a) == re.sub(r"\d", "", b):
        return None
    if shorter and longer.endswith(shorter):
        return None  # a prefix like un- or re- makes another word
    letters = edit_distance(a, b)
    return (index, letters) if letters <= MAX_TYPO_LETTERS and letters * 2 < len(shorter) else None


def mapped_together(mappings: list[RequestMapping], others: list[RequestMapping]) -> bool:
    """Both paths are in one annotation, like @GetMapping({"/users", "/users/"}): mapped so on purpose."""
    return bool({(m.file, m.line) for m in mappings} & {(m.file, m.line) for m in others})


def with_segment(path: str, index: int, segment: str) -> str:
    parts = segments(path)
    parts[index] = segment
    return "/" + "/".join(parts)


def slash_near_duplicate(project: SpringProject, mappings: list[RequestMapping], others: list[RequestMapping]) -> CheckResult:
    with_slash = next(m for m in mappings + others if m.path.endswith("/"))
    without = with_other_slash(with_slash.path)
    return CheckResult(
        False,
        f"{without} and {with_slash.path} differ only by the / at the end:\n{mapping_rows(mappings + others)}",
        f"if they are meant to be one URL, {change_path_fix(project, with_slash, without)}",
        with_slash.file,
        with_slash.line,
        note=slash_note(without),
    )


def typo_near_duplicate(
    project: SpringProject, mappings: list[RequestMapping], others: list[RequestMapping], index: int, letters: int
) -> CheckResult:
    """Change the path whose spelling the other mappings use less often."""
    used = Counter(part for m in known_mappings(project) for part in segments(m.path))
    first, second = mappings[0], others[0]
    if used[segments(first.path)[index]] < used[segments(second.path)[index]]:
        first, second = second, first
    target = with_segment(second.path, index, segments(first.path)[index])
    return CheckResult(
        False,
        f"{first.path} and {second.path} are {letters} letter{'s' if letters > 1 else ''} apart, "
        f"which is often a typo:\n{mapping_rows(mappings + others)}",
        f"if they are meant to be one path, {change_path_fix(project, second, target)}",
        second.file,
        second.line,
    )


def mappings_are_not_near_duplicates(project: SpringProject) -> list[CheckResult]:
    by_shape: dict[str, list[RequestMapping]] = {}
    for mapping in known_mappings(project):
        by_shape.setdefault(path_shape(mapping.path), []).append(mapping)
    shapes = list(by_shape)
    failed = []
    for i, shape in enumerate(shapes):
        for other in shapes[i + 1 :]:
            mappings, others = by_shape[shape], by_shape[other]
            if mapped_together(mappings, others):
                continue
            if with_other_slash(shape) == other:
                failed.append(slash_near_duplicate(project, mappings, others))
            elif typo := typo_segment(shape, other):
                failed.append(typo_near_duplicate(project, mappings, others, *typo))
    if failed:
        return failed
    return [CheckResult(True, "no two paths differ only by a typo or a / at the end")] if shapes else []


# --- entities ---


def entities(project: SpringProject) -> list[JavaType]:
    return [t for t in project.types if annotation_named(t.annotations, "Entity")]


def failures(results: list[CheckResult | None]) -> list[CheckResult]:
    return [r for r in results if r is not None and not r.passed]


def entities_are_valid(project: SpringProject) -> list[CheckResult]:
    app = project.application
    found = entities(project)
    failed = []
    for entity in found:
        details = {"class_name": entity.full_name}
        failed += failures([entity_uses_jakarta_persistence(project, details), entity_has_id(project, details)])
        if app and not is_scanned(entity, entity_packages(app)):
            how = f'add @EntityScan("{entity.package}") to {app.name}'
            failed.append(outside_packages(entity, app, entity_packages(app), "entities", how))
    if failed:
        return failed
    if not found:
        return []
    return [
        CheckResult(
            True,
            "every entity uses jakarta.persistence, has an @Id and is in a package Spring Boot scans "
            f"({counted(len(found), 'entity', 'entities')})",
        )
    ]


def stored_entity(repository: JavaType) -> str | None:
    """The entity a Spring Data repository stores: User for JpaRepository<User, Long>."""
    for parent in repository.extends:
        if simple_type_name(parent) in SPRING_DATA_REPOSITORIES and (found := re.search(r"<\s*([\w.$]+)", parent)):
            return found[1]
    return None


def repositories_store_entities(project: SpringProject) -> list[CheckResult]:
    repositories = [(b.declared_in, name) for b in type_beans(project) if b.reason.startswith("extends") and (name := stored_entity(b.declared_in))]
    failed = []
    checked = 0
    for repository, name in repositories:
        if find_type(project, name) is None:
            continue  # a type variable, or an entity from a library
        checked += 1
        result = class_has_entity_annotation(project, {"class_name": name})
        if not result.passed:
            failed.append(replace(result, message=f"{result.message}, but {repository.name} stores it"))
    if failed:
        return failed
    if not checked:
        return []
    return [CheckResult(True, f"every repository stores an @Entity class ({counted(checked, 'repository', 'repositories')})")]


# --- database ---


def profile_properties(root: Path) -> list[Property]:
    """The keys in application-<profile>.properties and .yml, which a profile may switch on."""
    found = []
    for path in sorted((root / RESOURCES).glob("application-*")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".properties":
            found += properties_file(text, path.relative_to(root))
        elif path.suffix in (".yml", ".yaml"):
            found += yaml_file(text, path.relative_to(root))
    return found


def datasource_url(project: SpringProject) -> Property | None:
    key = "spring.datasource.url"
    return project.property(key) or next((p for p in profile_properties(project.root) if p.key == key), None)


def properties_path(project: SpringProject) -> str:
    return next((p.file for p in project.properties), RESOURCES / "application.properties").as_posix()


def jpa_has_datasource(project: SpringProject) -> list[CheckResult]:
    build = build_file(project.root)
    if build is None:
        return []
    dependencies = [d for d in read_dependencies(project.root) if not d.for_tests_only]
    jpa = next((d for d in dependencies if d.artifact in JPA_STARTERS), None)
    if jpa is None:
        return [CheckResult(True, f"{build.as_posix()} has no JPA starter, so the app needs no database")]
    if url := datasource_url(project):
        return [CheckResult(True, f"spring.datasource.url = {url.value}", file=url.file, line=url.line)]
    if embedded := next((d for d in dependencies if d.artifact in EMBEDDED_DATABASES), None):
        name = EMBEDDED_DATABASES[embedded.artifact]
        return [CheckResult(True, f"no spring.datasource.url, so Spring Boot starts an in-memory {name} database", file=embedded.file, line=embedded.line)]
    if docker := next((d for d in dependencies if d.artifact in DOCKER_DATABASES), None):
        return [CheckResult(True, f"no spring.datasource.url: {docker.artifact} starts the database in Docker", file=docker.file, line=docker.line)]
    if os.environ.get("SPRING_DATASOURCE_URL"):
        return [CheckResult(True, "spring.datasource.url comes from the SPRING_DATASOURCE_URL environment variable")]
    return [
        CheckResult(
            False,
            f"{build.as_posix()} has {jpa.artifact}, and no database is set up: "
            "no spring.datasource.url, and no in-memory database such as H2",
            f"add spring.datasource.url, with spring.datasource.username and password, to {properties_path(project)}; "
            f"or, to start with an in-memory database, add H2 to {build.as_posix()}: {H2_DEPENDENCY[build.name]}",
            jpa.file,
            jpa.line,
            note="without a database the app stops at startup, because JPA has no DataSource to connect to",
        )
    ]


# --- port ---


def port_accepts_connections(port: int) -> bool:
    """Whether a program on this machine already listens on `port`."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=CONNECT_SECONDS):
                return True
        except OSError:
            continue
    return False


def server_port_is_free(project: SpringProject) -> list[CheckResult]:
    setting = project.property("server.port")
    value = setting.value if setting else str(DEFAULT_PORT)
    if found := PLACEHOLDER_DEFAULT.fullmatch(value):
        value = found["default"]  # ${PORT:8080}: 8080 unless PORT is set
    elif value.startswith("${"):
        return [CheckResult(True, f"server.port = {setting.value}, set when the app starts", file=setting.file, line=setting.line)]
    if not value.isdigit():
        return [property_has_valid_value(project, {"property": "server.port", "value": value, "target_type": "java.lang.Integer"})]
    port = int(value)
    where = {"file": setting.file, "line": setting.line} if setting else {}
    if port == 0:
        return [CheckResult(True, "server.port=0: Spring Boot picks a free port each time", **where)]
    if port_accepts_connections(port):
        return [port_is_free(project, {"port": str(port)})]
    source = "server.port" if setting else "the default; server.port is not set"
    return [CheckResult(True, f"port {port} is free ({source})", **where)]


# --- all of them ---

SECTIONS: list[tuple[str, list[Callable[[SpringProject], list[CheckResult]]]]] = [
    ("Beans", [beans_are_scanned, injections_have_beans]),
    ("Mappings", [mappings_are_unique, mappings_are_not_near_duplicates]),
    ("Entities", [entities_are_valid, repositories_store_entities]),
    ("Database", [jpa_has_datasource]),
    ("Port", [server_port_is_free]),
]


def run_section(project: SpringProject, title: str, checks: list[Callable[[SpringProject], list[CheckResult]]]) -> Section:
    section = Section(title)
    for check in checks:
        try:
            section.results += check(project)
        except Exception as error:
            section.errors.append(f"fsd: could not run {check.__name__}: {error!r}")
    return section


def examine(root: Path | str = ".") -> list[Section]:
    """Run every doctor check on the project in `root`, by section."""
    project = scan(root)
    return [run_section(project, title, checks) for title, checks in SECTIONS]
