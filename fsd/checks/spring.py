"""Startup and request checklists: small checks that look for a failure's cause in the project's source.

Each check takes the scanned project and the failure's details from the log,
and returns a CheckResult, or None when it does not apply (say, an annotation
check on an interface). A checklist runs its checks in order and stops at the
first one that fails: that one says what is wrong and how to fix it.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from fsd.checks.paths import (
    SYNONYMS,
    differences,
    ends_with_slash,
    filled,
    matches,
    path_distance,
    query_value,
    synonym_variants,
    variable_value,
    wanted,
    with_other_slash,
    with_query_value,
    with_variable,
    without_query,
)
from fsd.scanner.properties import RESOURCES
from fsd.scanner.spring import (
    MAPPING_ANNOTATIONS,
    Annotation,
    Bean,
    Field,
    JavaType,
    Method,
    Parameter,
    RequestMapping,
    SpringProject,
    annotation_named,
    first_string,
    scan,
    simple_type_name,
    with_leading_slash,
)
from fsd.tracker import Failure, Trace

# The annotation to suggest for a class, by the end of its name; @Component for any other.
BEAN_ANNOTATIONS = [
    (("Repository", "Dao"), "@Repository", "org.springframework.stereotype.Repository"),
    (("Controller",), "@RestController", "org.springframework.web.bind.annotation.RestController"),
    (("Service",), "@Service", "org.springframework.stereotype.Service"),
    (("Config", "Configuration"), "@Configuration", "org.springframework.context.annotation.Configuration"),
]
COMPONENT = ("@Component", "org.springframework.stereotype.Component")
# Library types Spring Boot never creates a bean for, so the app has to.
LIBRARY_BEAN_FIXES = {
    "RestTemplate": "Spring Boot does not create a RestTemplate for you. Add a @Bean method that returns one, "
    "for example in a @Configuration class: @Bean RestTemplate restTemplate() { return new RestTemplate(); }",
}
CRUD_METHODS = ["save", "saveAll", "findById", "existsById", "findAll", "findAllById", "count",
                "deleteById", "delete", "deleteAllById", "deleteAll"]  # fmt: skip
# The methods a repository gets from each Spring Data interface it extends.
SPRING_DATA_METHODS = {
    "Repository": [],
    "CrudRepository": CRUD_METHODS,
    "ListCrudRepository": CRUD_METHODS,
    "PagingAndSortingRepository": ["findAll"],
    "ListPagingAndSortingRepository": ["findAll"],
    "JpaRepository": CRUD_METHODS + ["flush", "saveAndFlush", "saveAllAndFlush", "deleteAllInBatch",
                                     "deleteAllByIdInBatch", "getReferenceById"],
}  # fmt: skip
# Where a property can be fixed, by the type Spring could not convert it to: (what it needs, an example).
EXPECTED_VALUES = [
    ({"Integer", "int", "Long", "long", "Short", "short", "Byte", "byte"}, "a whole number", "10"),
    ({"Boolean", "boolean"}, "true or false", "true"),
    ({"Double", "double", "Float", "float", "BigDecimal"}, "a number", "1.5"),
    ({"Duration"}, "a duration", "30s"),
    ({"DataSize"}, "a size", "10MB"),
]
DATABASE_NAMES = {"h2": "the H2 server", "mysql": "MySQL", "mariadb": "MariaDB", "postgresql": "PostgreSQL",
                  "sqlserver": "SQL Server", "oracle": "Oracle"}  # fmt: skip
JDBC_URL = re.compile(r"jdbc:(?P<kind>\w+)(?::(?P<mode>tcp|ssl))?.*?//(?P<host>[^/:;?]+)(?::(?P<port>\d+))?")
SYMBOL_OWNER = re.compile(r"(?:of type|^(?:class|interface|enum|record))\s+(?P<type>[\w.$]+)")
HANDLER = re.compile(r"(?P<owner>[\w.$]+)#(?P<method>\w+)\(")
LOGGED_FILE = re.compile(r"\[(?P<name>[^\]]+)\]")
# The mapping annotation for each HTTP method: @GetMapping for GET.
METHOD_ANNOTATIONS = {method: f"@{name}" for name, method in MAPPING_ANNOTATIONS.items() if method}
REST_CONTROLLER = ("@RestController", "org.springframework.web.bind.annotation.RestController")
BARE_ANNOTATION = re.compile(r"@[\w.]+(?:\(\s*\))?")
RESTART_FIX = "restart the app: it is running older code than your source"
MAX_MAPPING_ROWS = 10


@dataclass
class CheckResult:
    passed: bool
    message: str  # what was found; a failed check can add lines, such as the source line
    fix: str = ""
    file: Path | None = None  # relative to the project folder
    line: int | None = None
    note: str = ""  # background worth knowing, shown under the fix

    @property
    def location(self) -> str:
        if self.file is None:
            return ""
        return f"{self.file}:{self.line}" if self.line else str(self.file)


Check = Callable[[SpringProject, dict[str, str]], CheckResult | None]


# --- reading the project ---


def find_type(project: SpringProject, name: str) -> JavaType | None:
    """The type a log names: by full name if it has a package, else by simple name."""
    name = name.replace("$", ".")
    found = project.types_named(name)
    return found[0] if found else None


def read_line(project: SpringProject, file: Path, line: int) -> str | None:
    try:
        lines = (project.root / file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return lines[line - 1] if 0 < line <= len(lines) else None


def declaration(project: SpringProject, java_type: JavaType) -> str:
    """The source line that declares `java_type`, like: public class UserService {"""
    text = read_line(project, java_type.file, java_type.line)
    return text.strip().rstrip("{").strip() if text else f"{java_type.kind} {java_type.name}"


def is_under(package: str, base: str) -> bool:
    return not base or package == base or package.startswith(base + ".")


def declared_bean(project: SpringProject, java_type: JavaType) -> Bean | None:
    """The bean Spring creates from `java_type` itself (not from a @Bean method), if any."""
    return next((b for b in project.beans if b.declared_in is java_type and b.reason != "@Bean method"), None)


def imports_name(java_type: JavaType, name: str) -> bool:
    package = name.rsplit(".", 1)[0]
    return any(i.name in (name, f"{package}.*") for i in java_type.imports)


def add_annotation_fix(project: SpringProject, java_type: JavaType, annotation: str, import_name: str) -> str:
    fix = f'add {annotation} on the line above "{declaration(project, java_type)}" ({java_type.file.name}:{java_type.line})'
    if not imports_name(java_type, import_name):
        fix += f", with import {import_name};"
    return fix


def suggested_bean_annotation(class_name: str) -> tuple[str, str]:
    """@Service for UserService or UserServiceImpl, @Repository for UserDao, and so on."""
    base = class_name.removesuffix("Impl")
    for endings, annotation, import_name in BEAN_ANNOTATIONS:
        if base.endswith(endings):
            return annotation, import_name
    return COMPONENT


def is_scanned_fix(java_type: JavaType, app: JavaType, how: str) -> str:
    return f"move {java_type.name} into {app.package} or a package below it, or {how}"


# --- missing bean: "required a bean of type 'X' that could not be found" ---


def bean_type_exists(project: SpringProject, details: dict[str, str]) -> CheckResult:
    wanted = details["bean_type"]
    if java_type := find_type(project, wanted):
        return CheckResult(True, f"{java_type.kind} {java_type.name} exists", file=java_type.file, line=java_type.line)
    name = simple_type_name(wanted)
    fix = LIBRARY_BEAN_FIXES.get(
        name, f"add the Spring Boot starter that provides {name} to pom.xml, or a @Bean method that returns one"
    )
    return CheckResult(False, f"{name} is not in your source code: it comes from a library ({wanted})", fix)


def bean_type_is_annotated(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    java_type = find_type(project, details["bean_type"])
    bean = declared_bean(project, java_type) if java_type else None
    if java_type is None or (java_type.kind == "interface" and bean is None):
        return None  # an interface needs an annotated class that implements it; checked last
    if bean:
        return CheckResult(True, f"{java_type.name} is a bean: {bean.reason}", file=java_type.file, line=java_type.line)
    annotation, import_name = suggested_bean_annotation(java_type.name)
    return CheckResult(
        False,
        f"{java_type.name} has no {annotation}, @Component or other bean annotation",
        add_annotation_fix(project, java_type, annotation, import_name),
        java_type.file,
        java_type.line,
    )


def bean_type_is_scanned(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    java_type = find_type(project, details["bean_type"])
    app = project.application
    if java_type is None or app is None or declared_bean(project, java_type) is None:
        return None
    return package_is_scanned(java_type, app, "beans", f'add scanBasePackages = "{java_type.package}" to @SpringBootApplication in {app.name}')


def package_is_scanned(java_type: JavaType, app: JavaType, what: str, how: str) -> CheckResult:
    if is_under(java_type.package, app.package):
        return CheckResult(True, f"{java_type.name} is in {java_type.package}, under {app.package}", file=java_type.file, line=java_type.line)
    return CheckResult(
        False,
        f"{java_type.name} is in {java_type.package}, outside {app.package} where Spring Boot looks for {what}",
        is_scanned_fix(java_type, app, how),
        java_type.file,
        java_type.line,
    )


def implementations(project: SpringProject, interface: JavaType) -> list[JavaType]:
    return [
        t
        for t in project.types
        if t.kind in ("class", "record") and interface.name in [simple_type_name(i) for i in t.implements]
    ]


def interface_has_bean_implementation(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    interface = find_type(project, details["bean_type"])
    if interface is None or interface.kind != "interface" or declared_bean(project, interface):
        return None
    classes = implementations(project, interface)
    if not classes:
        return CheckResult(
            False,
            f"{interface.name} is an interface and no class implements it",
            f"write a class that implements {interface.name} and add @Service to it",
            interface.file,
            interface.line,
        )
    beans = [c for c in classes if declared_bean(project, c)]
    if not beans:
        unannotated = classes[0]
        annotation, import_name = suggested_bean_annotation(unannotated.name)
        return CheckResult(
            False,
            f"{unannotated.name} implements {interface.name} but has no bean annotation",
            add_annotation_fix(project, unannotated, annotation, import_name),
            unannotated.file,
            unannotated.line,
        )
    app = project.application
    scanned = [b for b in beans if app is None or is_under(b.package, app.package)]
    if not scanned:
        return package_is_scanned(beans[0], app, "beans", f'add scanBasePackages = "{beans[0].package}" to @SpringBootApplication in {app.name}')
    return CheckResult(True, f"{scanned[0].name} implements {interface.name} and is a bean", file=scanned[0].file, line=scanned[0].line)


# --- missing entity: "Not a managed type: class X" ---


def entity_class(project: SpringProject, details: dict[str, str]) -> JavaType | None:
    return find_type(project, details["class_name"])


def class_has_entity_annotation(project: SpringProject, details: dict[str, str]) -> CheckResult:
    java_type = entity_class(project, details)
    if java_type is None:
        return CheckResult(False, f"{details['class_name']} is not in your source code", "a repository's entity type must be one of your own @Entity classes")
    if annotation_named(java_type.annotations, "Entity"):
        return CheckResult(True, f"{java_type.name} has @Entity", file=java_type.file, line=java_type.line)
    return CheckResult(
        False,
        f"{java_type.name} has no @Entity",
        add_annotation_fix(project, java_type, "@Entity", "jakarta.persistence.Entity"),
        java_type.file,
        java_type.line,
    )


def entity_uses_jakarta_persistence(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    java_type = entity_class(project, details)
    if java_type is None:
        return None
    javax = [i for i in java_type.imports if i.name.startswith("javax.persistence.")]
    if not javax:
        return CheckResult(True, f"{java_type.name} uses jakarta.persistence, not javax.persistence", file=java_type.file, line=java_type.line)
    return CheckResult(
        False,
        f"{java_type.name} imports {javax[0].name}, which Spring Boot 3 and later ignore",
        f"change javax.persistence to jakarta.persistence in the imports of {java_type.file.name}",
        java_type.file,
        javax[0].line,
    )


def entity_has_id(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    java_type = entity_class(project, details)
    if java_type is None:
        return None
    for member in java_type.fields + java_type.methods:
        if annotation_named(member.annotations, "Id", "EmbeddedId"):
            return CheckResult(True, f"{java_type.name} has an @Id field: {member.name}", file=java_type.file, line=member.line)
    return CheckResult(
        False,
        f"{java_type.name} has no @Id field",
        f"add @Id (import jakarta.persistence.Id) above the field that identifies each row, "
        f"for example: @Id @GeneratedValue private Long id;",
        java_type.file,
        java_type.line,
    )


def entity_is_scanned(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    java_type = entity_class(project, details)
    app = project.application
    if java_type is None or app is None:
        return None
    return package_is_scanned(java_type, app, "entities", f'add @EntityScan("{java_type.package}") to {app.name}')


# --- duplicate mapping: "Ambiguous mapping. Cannot map ..." ---


def handler_location(project: SpringProject, handler: str) -> tuple[str, Path | None, int | None]:
    """A handler from the log, like com.demo.DisplayController#display(), with its file and mapping line."""
    found = HANDLER.match(handler)
    if not found:
        return handler, None, None
    label = f"{simple_type_name(found['owner'])}#{found['method']}()"
    controller = find_type(project, found["owner"])
    if controller is None:
        return label, None, None
    mapping = next((m for m in project.mappings if m.controller is controller and m.method == found["method"]), None)
    if mapping:
        return label, mapping.file, mapping.line
    method = next((m for m in controller.methods if m.name == found["method"]), None)
    return label, controller.file, method.line if method else controller.line


def mapping_is_unique(project: SpringProject, details: dict[str, str]) -> CheckResult:
    handlers = [handler_location(project, details[key]) for key in ("method", "existing_method") if key in details]
    handlers.sort(key=lambda handler: (str(handler[1]), handler[2] or 0))
    width = max(len(label) for label, _, _ in handlers)
    rows = [f"{label:<{width}}  {file}:{line}" if file else label for label, file, line in handlers]
    request = " ".join(part for part in (details.get("http_method"), details.get("path")) if part)
    _, file, line = handlers[0]
    return CheckResult(
        False,
        "\n".join([f"{request} is mapped by two methods:"] + rows),
        "change the path or the HTTP method of one of them, or remove one",
        file,
        line,
    )


# --- port in use: "Port 8080 was already in use" ---


@dataclass
class Listener:
    pid: int
    name: str  # the program, like java
    command: str  # its full command line


def output_of(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def listening_process(port: int) -> Listener | None:
    """The process listening on TCP `port`, from lsof (macOS and most Linux); None where it can't be told."""
    found = output_of(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"])
    pid = re.search(r"^p(\d+)$", found, re.MULTILINE)
    if not pid:
        return None
    name = re.search(r"^c(.*)$", found, re.MULTILINE)
    command = output_of(["ps", "-o", "command=", "-p", pid[1]]).strip()
    return Listener(int(pid[1]), name[1] if name else "", command)


def is_this_app(project: SpringProject, listener: Listener) -> bool:
    app = project.application
    return (app is not None and app.full_name in listener.command) or str(project.root) in listener.command


def server_port_fix(project: SpringProject, port: int) -> str:
    if current := project.property("server.port"):
        return f"change server.port in {current.file}:{current.line} to a free port, like {port + 1}"
    properties = next((p.file for p in project.properties), RESOURCES / "application.properties")
    return f"run this app on another port: add server.port={port + 1} to {properties}"


def port_is_free(project: SpringProject, details: dict[str, str]) -> CheckResult:
    port = int(details["port"])
    listener = listening_process(port)
    if listener is None:
        return CheckResult(
            False,
            f"port {port} is used by another program, most often another copy of this app still running",
            f"stop that program, or {server_port_fix(project, port)}",
        )
    who = f"{listener.name} (pid {listener.pid})"
    if is_this_app(project, listener):
        return CheckResult(
            False,
            f"port {port} is used by another copy of this app: {who}",
            f"stop the other copy (Ctrl+C in its terminal, or kill {listener.pid}), or {server_port_fix(project, port)}",
        )
    return CheckResult(
        False, f"port {port} is used by {who}", f"stop it (kill {listener.pid}), or {server_port_fix(project, port)}"
    )


# --- compile error: "[ERROR] File.java:[20,38] cannot find symbol" ---


def project_file(logged: str) -> Path:
    """The file a log names, relative to the project when it is under src/main/java.

    The log may come from another copy of the project, so only that part of the path is kept.
    """
    posix = logged.replace("\\", "/")
    index = posix.find("src/main/java/")
    return Path(posix[index:]) if index != -1 else Path(logged)


def with_caret(source_line: str, column: int) -> str:
    """The line without its indent, and a ^ under javac's column (javac counts a tab as up to 8 columns)."""
    expanded = source_line.expandtabs(8)
    indent = len(expanded) - len(expanded.lstrip())
    return f"{expanded.strip()}\n{' ' * max(column - 1 - indent, 0)}^"


def first_word(name: str) -> str:
    """find from findEverything."""
    found = re.match(r"[A-Za-z][a-z0-9]*", name)
    return found[0].lower() if found else ""


def similar_names(name: str, candidates: list[str], same_verb: bool = False, limit: int = 3) -> list[str]:
    """Names close to `name` in spelling; with `same_verb`, also those starting with the same word,
    like findAll for findEverything."""

    def closeness(candidate: str) -> float:
        return SequenceMatcher(None, name.lower(), candidate.lower()).ratio()

    def is_similar(candidate: str) -> bool:
        return closeness(candidate) >= 0.6 or (same_verb and first_word(candidate) == first_word(name))

    close = {c for c in candidates if c != name and is_similar(c)}
    return sorted(close, key=lambda c: (-closeness(c), c))[:limit]


def member_names(project: SpringProject, java_type: JavaType, kind: str, seen: set[str] | None = None) -> list[str]:
    """The methods (or fields) of `java_type`, including those it gets from its supertypes."""
    seen = seen if seen is not None else set()
    seen.add(java_type.full_name)
    names = [m.name for m in java_type.methods] if kind == "method" else [f.name for f in java_type.fields]
    for parent in java_type.extends + java_type.implements:
        parent_type = find_type(project, simple_type_name(parent))
        if parent_type and parent_type.full_name not in seen:
            names += member_names(project, parent_type, kind, seen)
        elif kind == "method":
            names += SPRING_DATA_METHODS.get(simple_type_name(parent), [])
    return names


def joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def cannot_find_symbol_fix(project: SpringProject, details: dict[str, str]) -> str:
    kind, _, symbol = details["symbol"].partition(" ")
    name = symbol.split("(")[0]
    if kind == "class":
        similar = similar_names(name, [t.name for t in project.types])
        if similar:
            return f"there is no class {name}. Did you mean {joined(similar)}? Check the spelling and the import"
        return f"there is no class {name} in your code: check the spelling, the import, or the dependency in pom.xml"
    owner_name = SYMBOL_OWNER.search(details.get("location", ""))
    owner = find_type(project, owner_name["type"]) if owner_name else None
    if owner is None:
        return f"check the spelling of {name}"
    suffix = "()" if kind == "method" else ""
    similar = similar_names(name, member_names(project, owner, kind), same_verb=True)
    if similar:
        return f"{owner.name} has no {kind} {name}. Did you mean {joined([s + suffix for s in similar])}?"
    return f"{owner.name} has no {kind} {name}: check the spelling, or add it to {owner.name}"


def source_compiles(project: SpringProject, details: dict[str, str]) -> CheckResult:
    file = project_file(details["file"])
    line, column = int(details["line"]), int(details["column"])
    error = f"cannot find {details['symbol']}" if "symbol" in details else details.get("error", "does not compile")
    message = f"{file.name}:{line} {error}"
    if source := read_line(project, file, line):
        message += "\n" + with_caret(source, column)
    fix = cannot_find_symbol_fix(project, details) if "symbol" in details else f"fix the code at {file.name}:{line}"
    return CheckResult(False, message, fix, file, line)


# --- bad property: "Failed to bind properties under 'server.port' to java.lang.Integer" ---


def expected_value(key: str, target_type: str) -> tuple[str, str]:
    """What `target_type` needs, in words, and an example value for `key`."""
    for types, needs, example in EXPECTED_VALUES:
        if target_type in types:
            return needs, "8080" if key.endswith("port") and needs == "a whole number" else example
    return f"a valid {target_type}", ""


def property_has_valid_value(project: SpringProject, details: dict[str, str]) -> CheckResult:
    key, value = details["property"], details.get("value", "")
    target = simple_type_name(details.get("target_type", ""))
    needs, example = expected_value(key, target)
    if found := project.property(key):
        file, line = found.file, found.line
    else:
        logged = LOGGED_FILE.search(details.get("origin", ""))
        file = Path(logged["name"]) if logged else None
        line = int(details["origin_line"]) if "origin_line" in details else None
    fix = f"use {needs}, for example {key}={example}" if example else f"set {key} to {needs}"
    return CheckResult(False, f"{key}={value} is not {needs}, which {target} needs", fix, file, line)


# --- database refused: "ConnectException: Connection refused" ---


def datasource_url_is_set(project: SpringProject, details: dict[str, str]) -> CheckResult:
    url = project.property("spring.datasource.url")
    if url is None:
        return CheckResult(
            False,
            "spring.datasource.url is not in application.properties or application.yml",
            "the URL comes from somewhere else, such as a SPRING_DATASOURCE_URL environment variable or a profile; "
            "check that it points at a running database",
        )
    return CheckResult(True, f"spring.datasource.url = {url.value}", file=url.file, line=url.line)


def database_is_running(project: SpringProject, details: dict[str, str]) -> CheckResult:
    url = project.property("spring.datasource.url")
    parsed = JDBC_URL.match(url.value) if url else None
    host = details.get("host") or (parsed["host"] if parsed else "")
    port = details.get("port") or (parsed and parsed["port"]) or ""
    where = f"{host}:{port}" if host and port else host or "the address in spring.datasource.url"
    kind = parsed["kind"] if parsed else ""
    if kind == "h2":
        fix = (
            f"start the H2 server on port {port or 9092}, or use an in-memory database instead: "
            "spring.datasource.url=jdbc:h2:mem:testdb"
        )
    else:
        fix = f"start {DATABASE_NAMES.get(kind, 'the database')} at {where}, or fix the host and port in spring.datasource.url"
    return CheckResult(
        False,
        f"no database is running at {where}: nothing accepted the connection",
        fix,
        url.file if url else None,
        url.line if url else None,
    )


# --- requests: comparing the request with the mappings in the source ---


def requested(details: dict[str, str]) -> tuple[str, str]:
    """The request's HTTP method and its path, without the query."""
    return details["http_method"], without_query(details["request_path"])


def known_mappings(project: SpringProject) -> list[RequestMapping]:
    """The mappings whose path is known: not one built from a constant the scanner cannot read."""
    return [m for m in project.mappings if "<" not in m.path]


def mappings_matching(project: SpringProject, path: str) -> list[RequestMapping]:
    return [m for m in known_mappings(project) if matches(m.path, path)]


def accepts(mapping: RequestMapping, method: str) -> bool:
    return mapping.http_method in (None, method)


def best_for(mappings: list[RequestMapping], method: str) -> RequestMapping:
    """The mapping for `method` if there is one, else the first."""
    return next((m for m in mappings if accepts(m, method)), mappings[0])


def mapping_label(mapping: RequestMapping) -> str:
    return f"{mapping.http_method} {mapping.path}" if mapping.http_method else f"{mapping.path} (any method)"


def mapping_rows(mappings: list[RequestMapping]) -> str:
    """One aligned row per mapping: GET /users  UserController#list()  UserController.java:26"""
    labels = [mapping_label(m) for m in mappings]
    handlers = [f"{m.handler}()" for m in mappings]
    label_width, handler_width = max(map(len, labels)), max(map(len, handlers))
    return "\n".join(
        f"{label:<{label_width}}  {handler:<{handler_width}}  {m.file.name}:{m.line}"
        for label, handler, m in zip(labels, handlers, mappings)
    )


def call(mapping: RequestMapping, path: str, method: str) -> str:
    """'call /users', or 'send POST /users' when the mapping is for another HTTP method."""
    return f"call {path}" if accepts(mapping, method) else f"send {mapping.http_method} {path}"


def browser_hint(method: str) -> str:
    return " (a browser's address bar can only send GET)" if method == "GET" else ""


def slash_note(path: str) -> str:
    other = with_other_slash(path)
    with_slash, without = (path, other) if ends_with_slash(path) else (other, path)
    return f'since Spring Boot 3, "{with_slash}" and "{without}" are different URLs (Spring Boot 2 treated them as one)'


def with_path(annotation: str, old: str, new_value: str) -> str | None:
    """`annotation` with its path `old` changed to the Java value `new_value`; None if it can't be told where."""
    if old and annotation.count(f'"{old}"') == 1:
        return annotation.replace(f'"{old}"', new_value)
    if not old and BARE_ANNOTATION.fullmatch(annotation):
        return f"{annotation.split('(')[0]}({new_value})"
    return None


def change_annotation_fix(
    project: SpringProject, file: Path, line: int, old: str, new_value: str, otherwise: str, aside: str = ""
) -> str:
    """'change @GetMapping("/a") to @GetMapping("/b") (A.java:9)', from the annotation as written on `line`."""
    text = read_line(project, file, line)
    rewritten = with_path(text.strip(), old, new_value) if text else None
    where = f"({file.name}:{line}{aside})"
    return f"change {text.strip()} to {rewritten} {where}" if rewritten else f"{otherwise} {where}"


def own_path_for(base_path: str, path: str) -> str | None:
    """The method's own path that gives `path` under the class-level `base_path`; None if there is none."""
    if not base_path:
        return path
    base = with_leading_slash(base_path)
    if path == base:
        return ""
    stem = base.removesuffix("/")
    return path[len(stem) :] if path.startswith(stem + "/") else None


def base_path_for(own_path: str, path: str) -> str | None:
    """The class-level path that gives `path` with the method's own `own_path`; None if there is none."""
    own = with_leading_slash(own_path)
    if not own:
        return path
    return path[: -len(own)] if path.endswith(own) and len(path) > len(own) else None


def change_path_fix(project: SpringProject, mapping: RequestMapping, path: str) -> str:
    """How to change `mapping` so it maps `path`: in the method's annotation, or else in the class's @RequestMapping."""
    otherwise = f'change the mapping so its path is "{path}"'
    own = own_path_for(mapping.base_path, path)
    if own is not None:
        return change_annotation_fix(project, mapping.file, mapping.line, mapping.own_path, f'"{own}"', otherwise)
    base = base_path_for(mapping.own_path, path)
    class_level = annotation_named(mapping.controller.annotations, "RequestMapping")
    if base is None or class_level is None:
        return f"{otherwise} ({mapping.file.name}:{mapping.line})"
    aside = f"; every path in {mapping.controller.name} starts with it"
    return change_annotation_fix(project, mapping.file, class_level.line, mapping.base_path, f'"{base}"', otherwise, aside)


def method_change_fix(project: SpringProject, mapping: RequestMapping, method: str) -> str:
    """'change @PostMapping("/a") to @GetMapping("/a") (A.java:9)', from the annotation as written."""
    text = (read_line(project, mapping.file, mapping.line) or "").strip()
    old, new = METHOD_ANNOTATIONS.get(mapping.http_method), METHOD_ANNOTATIONS.get(method)
    where = f"({mapping.file.name}:{mapping.line})"
    if old and new and re.match(re.escape(old) + r"\b", text):
        return f"change {text} to {new}{text[len(old):]} {where}"
    return f"add {method} to the methods of the mapping {where}"


# --- 404: nothing is mapped to the path ---


def mapping_is_registered(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    """The source maps the request, yet the app answered 404: Spring is not serving that mapping."""
    method, path = requested(details)
    mapped = [m for m in mappings_matching(project, path) if accepts(m, method)]
    if not mapped:
        return None
    mapping, controller = mapped[0], mapped[0].controller
    if not annotation_named(controller.annotations, "RestController", "Controller"):
        return CheckResult(
            False,
            f"{controller.name} maps {mapping_label(mapping)} but has no @RestController, so Spring never serves it",
            add_annotation_fix(project, controller, *REST_CONTROLLER),
            controller.file,
            controller.line,
        )
    app = project.application
    if app is not None and not is_under(controller.package, app.package):
        how = f'add scanBasePackages = "{controller.package}" to @SpringBootApplication in {app.name}'
        return package_is_scanned(controller, app, "controllers", how)
    return CheckResult(
        False, f"{mapping_label(mapping)} is in your source, but the app that answered does not have it", RESTART_FIX, mapping.file, mapping.line
    )


def path_has_other_method(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    others = [m for m in mappings_matching(project, path) if not accepts(m, method)]
    if not others:
        return CheckResult(True, f"no mapping has the path {path} with another HTTP method")
    mapping = others[0]
    return CheckResult(
        False,
        f"{path} is mapped for {joined(sorted({m.http_method for m in others}))}, not {method}",
        f"send {mapping.http_method} {path}{browser_hint(method)}, or {method_change_fix(project, mapping, method)}",
        mapping.file,
        mapping.line,
    )


def path_differs_by_trailing_slash(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    other = with_other_slash(path)
    found = mappings_matching(project, other)
    if not found:
        return CheckResult(True, f"no mapping differs from {path} only by the / at the end")
    mapping = best_for(found, method)
    own = mapping.own_path
    both = f'{{"{own}", "{own[:-1] if own.endswith("/") else own + "/"}"}}'
    return CheckResult(
        False,
        f"{mapping_label(mapping)} is mapped, and the request was for {path}: the / at the end makes it another URL",
        f"{call(mapping, other, method)}, or map both: "
        + change_annotation_fix(project, mapping.file, mapping.line, own, both, f"add {path} to the paths of the mapping"),
        mapping.file,
        mapping.line,
        note=slash_note(path),
    )


def path_differs_by_synonym(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    for variant, swaps in synonym_variants(path):
        if found := mappings_matching(project, variant):
            mapping = best_for(found, method)
            words = " and ".join(f'"{new}" where the request has "{old}"' for old, new in swaps)
            return CheckResult(
                False,
                f"{mapping_label(mapping)} is mapped: it says {words}",
                f"{call(mapping, variant, method)}, or {change_path_fix(project, mapping, wanted(mapping.path, path))}",
                mapping.file,
                mapping.line,
            )
    table = ", ".join("/".join(group) for group in SYNONYMS)
    return CheckResult(True, f"no mapping differs from {path} only by a synonym ({table})")


def is_close(distance: int, path: str) -> bool:
    return distance <= max(2, len(path) // 3)


def closest_mapping(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    mappings = known_mappings(project)
    add = f'add a method with {METHOD_ANNOTATIONS.get(method, "@RequestMapping")}("{path}") to a @RestController class'
    if not mappings:
        return CheckResult(False, "your source has no request mappings: no @GetMapping, @PostMapping or @RequestMapping", f"{add}, and run fsd in the project's folder")
    best = min(mappings, key=lambda m: (path_distance(path, m.path), not accepts(m, method)))
    if not is_close(path_distance(path, best.path), path):
        shown = mappings[:MAX_MAPPING_ROWS]
        more = f"\n... and {len(mappings) - len(shown)} more" if len(mappings) > len(shown) else ""
        return CheckResult(False, f"none of your {len(mappings)} mappings is close to {path}:\n{mapping_rows(shown)}{more}", f"call one of them, or {add}")
    target = filled(best.path, path)
    return CheckResult(
        False,
        f"the closest of your {len(mappings)} mappings is {mapping_label(best)}: {', '.join(differences(path, target))}",
        f"{change_path_fix(project, best, wanted(best.path, path))}, or {call(best, target, method)} instead",
        best.file,
        best.line,
        note=slash_note(path) if ends_with_slash(path) != ends_with_slash(target) else "",
    )


# --- 405: the path is mapped, but not for this HTTP method ---


def path_accepts_method(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    found = mappings_matching(project, path)
    if not found:
        return CheckResult(False, f"no mapping in your source matches {path}", RESTART_FIX)
    methods = joined(sorted({m.http_method for m in found if m.http_method}))
    first = found[0]
    annotation = METHOD_ANNOTATIONS.get(method, "@RequestMapping")
    add = f'{annotation}("{first.own_path}")' if first.own_path else annotation
    return CheckResult(
        False,
        f"{path} accepts {methods}, not {method}:\n{mapping_rows(found)}",
        f"send {methods} {path}{browser_hint(method)}, or add a method with {add} to {first.controller.name} for {method}",
        first.file,
        first.line,
    )


# --- 400: the request's data does not fit the handler ---


def handler_of(project: SpringProject, details: dict[str, str]) -> tuple[JavaType, Method] | None:
    """The controller method the request was mapped to, named by "Mapped to" or by the exception."""
    controller = find_type(project, details.get("controller", ""))
    method = next((m for m in controller.methods if m.name == details.get("handler")), None) if controller else None
    return (controller, method) if method else None


def body_parameter(method: Method) -> Parameter | None:
    return next((p for p in method.parameters if annotation_named(p.annotations, "RequestBody")), None)


def bound_name(parameter: Parameter, annotation: Annotation | None) -> str:
    """The name a @PathVariable or @RequestParam reads: q for @RequestParam("q") String query."""
    return (first_string(annotation.values("value", "name")) if annotation else None) or parameter.name


def value_parameter(method: Method, name: str | None, target_type: str = "") -> tuple[Parameter, Annotation | None] | None:
    """The parameter that reads the path or query value `name`; by its type when the log gives no name."""
    for parameter in method.parameters:
        annotation = annotation_named(parameter.annotations, "PathVariable", "RequestParam")
        if annotation is None and annotation_named(parameter.annotations, "RequestBody", "RequestHeader"):
            continue
        if name in (bound_name(parameter, annotation), parameter.name):
            return parameter, annotation
        if name is None and annotation and simple_type_name(parameter.type) == target_type:
            return parameter, annotation
    return None


def handler_mapping(project: SpringProject, controller: JavaType, method: Method, path: str) -> RequestMapping | None:
    return next(
        (m for m in project.mappings if m.controller is controller and m.method == method.name and matches(m.path, path)),
        None,
    )


def json_example(java_type: str) -> str | None:
    """A JSON value of `java_type`: "text" for String, 10 for Long; None for other types."""
    name = simple_type_name(java_type)
    if name == "String":
        return '"text"'
    needs, example = expected_value("", name)
    return example if needs in ("a whole number", "true or false", "a number") else None


def example_body(project: SpringProject, type_name: str) -> str:
    """A JSON body for `type_name` from its fields, like {"name": "text"}; ids and fields of other types are left out."""
    java_type = find_type(project, simple_type_name(type_name))
    if java_type is None:
        return "{}"
    values = [
        f'"{f.name}": {value}'
        for f in java_type.fields
        if "static" not in f.modifiers
        and not annotation_named(f.annotations, "Id", "GeneratedValue", "JsonIgnore")
        and (value := json_example(f.type))
    ]
    return "{" + ", ".join(values) + "}"


def request_has_body(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    found = handler_of(project, details)
    parameter = body_parameter(found[1]) if found else None
    if parameter is None:
        return CheckResult(False, "the handler reads a request body, and the request had none", "send a JSON body with the request")
    controller, handler = found
    return CheckResult(
        False,
        f"{controller.name}.{handler.name}() reads the body into @RequestBody {parameter.type} {parameter.name}, "
        "and the request had none",
        "send a JSON body with Content-Type: application/json, like: "
        f"fsd try {method} {path} --body '{example_body(project, parameter.type)}'. "
        "If the body may be left out, use @RequestBody(required = false)",
        controller.file,
        parameter.line,
    )


def body_is_read_by_handler(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    found = handler_of(project, details)
    parameter = body_parameter(found[1]) if found else None
    if parameter is None:
        return None
    controller, handler = found
    return CheckResult(
        True,
        f"{controller.name}.{handler.name}() reads the JSON body into {parameter.type} {parameter.name}",
        file=controller.file,
        line=parameter.line,
    )


def body_is_valid_json(project: SpringProject, details: dict[str, str]) -> CheckResult:
    found = handler_of(project, details)
    parameter = body_parameter(found[1]) if found else None
    example = example_body(project, parameter.type) if parameter else '{"name": "text"}'
    return CheckResult(
        False,
        f"the body is not valid JSON: {details.get('json_error', 'JSON parse error')}",
        f"check the body's brackets, quotes and commas: every {{ needs its }}, "
        f"and names and text need double quotes, like {example}",
    )


def json_field_has_right_type(project: SpringProject, details: dict[str, str]) -> CheckResult:
    """Jackson does not say which field it was, so the fields of the type it needed are the suspects."""
    target = simple_type_name(details["target_type"])
    needs, _ = expected_value("", target)
    sent = details.get("json_value") or f"a JSON {details.get('json_kind', 'value').lower()}"
    found = handler_of(project, details)
    parameter = body_parameter(found[1]) if found else None
    body_type = find_type(project, simple_type_name(parameter.type)) if parameter else None
    fields = [f for f in body_type.fields if simple_type_name(f.type) == target] if body_type else []
    message = f"the body has {sent}, which is not a valid {target}"
    if not fields:
        return CheckResult(False, message, f"send {needs} there")
    if len(fields) == 1:
        message += f"; the only {target} field of {body_type.name} is {fields[0].name}"
    else:
        names = ", ".join(f.name for f in fields[:-1]) + f" and {fields[-1].name}"
        message += f"; the {target} fields of {body_type.name} are {names}"
    example = json_example(target)
    fix = f"send {needs} for " + joined([f'"{f.name}"' for f in fields])
    if example and len(fields) == 1:
        fix += f', like "{fields[0].name}": {example}'
    return CheckResult(False, message, fix, body_type.file, fields[0].line)


def parameter_has_right_type(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    target = simple_type_name(details["target_type"])
    needs, _ = expected_value("", target)
    example = (json_example(target) or "").strip('"')
    found = handler_of(project, details)
    bound = value_parameter(found[1], details.get("parameter"), target) if found else None
    if bound is None:
        name = details.get("parameter", "the value")
        return CheckResult(False, f"{name} must be {needs} ({target})", f"send {needs} for {name}")
    (controller, handler), (parameter, annotation) = found, bound
    name, declared = bound_name(parameter, annotation), f"{parameter.type} {parameter.name}"
    mapping = handler_mapping(project, controller, handler, path)
    if annotation is not None and annotation.name == "PathVariable" and mapping is not None:
        value = details.get("value") or variable_value(mapping.path, path, name)
        message = f'{{{name}}} in {mapping.path} is read into {declared}, so it must be {needs}; the request has "{value}"'
        fixed = with_variable(mapping.path, path, name, example) if example else None
    else:
        value = details.get("value") or query_value(details["request_path"], name) or ""
        message = f'?{name}= is read into {declared}, so it must be {needs}; the request has "{value}"'
        fixed = with_query_value(details["request_path"], name, example) if example else None
    asked = f"call {fixed}" if fixed else f"send {needs} for {name}"
    return CheckResult(
        False, message, f"{asked}, or, if any text is allowed there, change {declared} to String {parameter.name}", controller.file, parameter.line
    )


def request_param_is_present(project: SpringProject, details: dict[str, str]) -> CheckResult:
    method, path = requested(details)
    name = details["parameter"]
    example = (json_example(details.get("parameter_type", "String")) or "...").strip('"')
    asked = f"call {with_query_value(details['request_path'], name, example)}"
    found = handler_of(project, details)
    bound = value_parameter(found[1], name) if found else None
    if bound is None:
        return CheckResult(False, f"the handler needs ?{name}=, and the request had none", asked)
    (controller, handler), (parameter, _) = found, bound
    named = f'name = "{name}", ' if name != parameter.name else ""
    declared = f"{parameter.type} {parameter.name}"
    return CheckResult(
        False,
        f"{controller.name}.{handler.name}() needs ?{name}= for @RequestParam {declared}, and the request had none",
        f"{asked}, or make it optional: @RequestParam({named}required = false) {declared}, "
        f'or give it a default: @RequestParam({named}defaultValue = "{example}") {declared}',
        controller.file,
        parameter.line,
    )


# --- SQL: from the database's names back to entities and fields ---

SQL_TABLE = re.compile(
    r"\b(?:from|join|into|update)\s+(?P<table>[\w.]+)"
    r"(?:\s+(?:as\s+)?(?!(?:where|set|values|on|left|right|inner|outer|cross|join|order|group|limit)\b)(?P<alias>\w+))?",
    re.IGNORECASE,
)
RELATIONSHIPS = ("OneToMany", "ManyToOne", "OneToOne", "ManyToMany")
# A column type to suggest, by the field's Java type.
SQL_TYPES = [
    ({"String", "char", "Character"}, "varchar(255)"),
    ({"Long", "long"}, "bigint"),
    ({"Integer", "int", "Short", "short", "Byte", "byte"}, "integer"),
    ({"Boolean", "boolean"}, "boolean"),
    ({"Double", "double", "Float", "float"}, "double precision"),
    ({"BigDecimal"}, "numeric(19,2)"),
    ({"LocalDate"}, "date"),
    ({"LocalDateTime", "Instant", "OffsetDateTime"}, "timestamp"),
]
JSON_ANNOTATIONS = "com.fasterxml.jackson.annotation"


def snake_case(name: str) -> str:
    """The table or column name Spring Boot gives a Java name: firstName -> first_name, UserAccount -> user_account."""
    text = name.replace(".", "_")
    out = text[:1]
    for i in range(1, len(text)):
        before, current, after = text[i - 1], text[i], text[i + 1 : i + 2]
        if (before.islower() or before.isdigit()) and current.isupper() and (after.islower() or after.isdigit()):
            out += "_"
        out += current
    return out.lower()


def entities(project: SpringProject) -> list[JavaType]:
    return [t for t in project.types if annotation_named(t.annotations, "Entity")]


def table_name(entity: JavaType) -> str:
    """@Table(name = ...), else @Entity(name = ...), else the class name, as Spring Boot writes it."""
    for annotation_name in ("Table", "Entity"):
        annotation = annotation_named(entity.annotations, annotation_name)
        if annotation and (name := first_string(annotation.values("name"))):
            return snake_case(name)
    return snake_case(entity.name)


def entity_for_table(project: SpringProject, table: str) -> JavaType | None:
    return next((e for e in entities(project) if table_name(e) == table.lower()), None)


def id_field(entity: JavaType) -> Field | None:
    return next((f for f in entity.fields if annotation_named(f.annotations, "Id", "EmbeddedId")), None)


def joined_entity(project: SpringProject, field: Field) -> JavaType | None:
    """The entity a @ManyToOne or @OneToOne field points at."""
    if not annotation_named(field.annotations, "ManyToOne", "OneToOne"):
        return None
    target = find_type(project, simple_type_name(field.type))
    return target if target and annotation_named(target.annotations, "Entity") else None


def column_name(project: SpringProject, field: Field) -> str:
    """@Column or @JoinColumn's name, else the field's name; a join column adds the other table's id: user_id."""
    column = annotation_named(field.annotations, "Column", "JoinColumn")
    if column and (name := first_string(column.values("name"))):
        return snake_case(name)
    target = joined_entity(project, field)
    target_id = id_field(target) if target else None
    if target_id:
        return f"{snake_case(field.name)}_{column_name(project, target_id)}"
    return snake_case(field.name)


def stored_fields(entity: JavaType) -> list[Field]:
    """The fields with a column in the entity's own table."""
    def is_stored(f: Field) -> bool:
        one_to_one = annotation_named(f.annotations, "OneToOne")
        return (
            "static" not in f.modifiers
            and "transient" not in f.modifiers
            and not annotation_named(f.annotations, "Transient", "OneToMany", "ManyToMany")
            and not (one_to_one and one_to_one.values("mappedBy"))
        )

    return [f for f in entity.fields if is_stored(f)]


def field_for_column(project: SpringProject, entity: JavaType, column: str) -> Field | None:
    return next((f for f in stored_fields(entity) if column_name(project, f) == column.lower()), None)


def column_definition(project: SpringProject, field: Field) -> str:
    """email varchar(255), or user_id bigint for a join column; the type is left out when it is not known."""
    target = joined_entity(project, field)
    target_id = id_field(target) if target else None
    java_type = simple_type_name(target_id.type if target_id else field.type)
    sql_type = next((sql for types, sql in SQL_TYPES if java_type in types), "")
    return f"{column_name(project, field)} {sql_type}".strip()


def statement_table(details: dict[str, str]) -> str | None:
    """The table a failure is about: named by the database, or found from the column's alias in the statement."""
    if "table" in details:
        return details["table"].lower()
    tables = {(found["alias"] or "").lower(): found["table"].split(".")[-1].lower() for found in SQL_TABLE.finditer(details.get("sql", ""))}
    return tables.get(details.get("alias", "").lower()) or next(iter(tables.values()), None)


def schema_script(project: SpringProject, table: str) -> tuple[Path, int | None] | None:
    """The project's schema.sql, if it has one, and the line where it creates `table`."""
    script = RESOURCES / "schema.sql"
    try:
        lines = (project.root / script).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    creates = re.compile(rf'\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?(?:\w+\.)?"?{re.escape(table)}"?(?![\w"])', re.IGNORECASE)
    return script, next((number for number, text in enumerate(lines, start=1) if creates.search(text)), None)


def ddl_auto_note(project: SpringProject) -> str:
    setting = project.property("spring.jpa.hibernate.ddl-auto")
    if setting is None or setting.value not in ("none", "validate"):
        return ""
    return (
        f"spring.jpa.hibernate.ddl-auto={setting.value} ({setting.file}:{setting.line}) leaves the schema to you; "
        "with update, Hibernate adds the tables and columns that are missing"
    )


def repository_for(project: SpringProject, entity: JavaType) -> JavaType | None:
    """The Spring Data repository of `entity`: UserRepository for JpaRepository<User, Long>."""
    held = re.compile(rf"<\s*(?:[\w.]+\.)?{re.escape(entity.name)}\s*[,>]")
    return next((t for t in project.types if t.kind == "interface" and any(held.search(e) for e in t.extends)), None)


# --- SQL errors: missing table or column, duplicate key, lazy loading ---


def table_has_entity(project: SpringProject, details: dict[str, str]) -> CheckResult:
    table = statement_table(details)
    entity = entity_for_table(project, table) if table else None
    if entity is None:
        return CheckResult(False, f"no entity in your source is stored in table {table}", "check the table name in the query")
    return CheckResult(True, f"table {table} belongs to entity {entity.name}", file=entity.file, line=entity.line)


def column_has_field(project: SpringProject, details: dict[str, str]) -> CheckResult | None:
    table = statement_table(details)
    entity = entity_for_table(project, table) if table else None
    if entity is None:
        return None
    column = details["column"].lower()
    field = field_for_column(project, entity, column)
    if field is None:
        return CheckResult(
            False, f"no field of {entity.name} is stored in column {column}", f"check the column name in the query, or add {column} to table {table}"
        )
    definition = column_definition(project, field)
    script = schema_script(project, table)
    if script and script[1]:
        add = f'add {definition} to "create table {table}" in {script[0].name}:{script[1]}'
    else:
        add = f"add the column to the database: alter table {table} add column {definition}"
    return CheckResult(
        False,
        f"{entity.name}.{field.name} is stored in column {column}, and table {table} has no such column",
        f"{add}; or, if {field.name} is not meant to be stored, mark it @Transient",
        entity.file,
        field.line,
        note=ddl_auto_note(project),
    )


def table_exists(project: SpringProject, details: dict[str, str]) -> CheckResult:
    table = details["table"].lower()
    entity = entity_for_table(project, table)
    if entity is None:
        return CheckResult(False, f"no entity in your source is stored in table {table}", "check the table name in the query")
    key = id_field(entity)
    columns = [column_definition(project, f) + (" primary key" if f is key else "") for f in stored_fields(entity)]
    create = f"create table {table} ({', '.join(columns)});"
    generated = annotation_named(key.annotations, "GeneratedValue") if key else None
    if generated and not any("IDENTITY" in value for value in generated.values("strategy")):
        create += f" create sequence {table}_seq start with 1 increment by 50;"  # Hibernate's default for @GeneratedValue
    script = schema_script(project, table)
    fix = f"add it to {script[0].name}: {create}" if script else f"create it in the database: {create}"
    table_annotation = annotation_named(entity.annotations, "Table")
    return CheckResult(
        False,
        f"{entity.name} is stored in table {table}, and the database has no such table",
        fix,
        entity.file,
        table_annotation.line if table_annotation else entity.line,
        note=ddl_auto_note(project),
    )


def unique_by(entity: JavaType, field: Field, column: str) -> str:
    """Where the source makes the column unique: @Column(unique = true), @Id, or @Table(uniqueConstraints = ...)."""
    if annotation_named(field.annotations, "Id", "EmbeddedId"):
        return "it is the @Id"
    column_annotation = annotation_named(field.annotations, "Column")
    if column_annotation and column_annotation.values("unique") == ["true"]:
        return "@Column(unique = true)"
    table = annotation_named(entity.annotations, "Table")
    if table and any(column in value.lower() for value in table.values("uniqueConstraints")):
        return "@Table(uniqueConstraints = ...)"
    return "a unique index in the database"


def unique_column_has_field(project: SpringProject, details: dict[str, str]) -> CheckResult:
    table = details["table"].lower()
    columns = [part.split()[0].lower() for part in details.get("columns", details.get("column", "")).split(",") if part.strip()]
    value = details.get("value", "the same value")
    entity = entity_for_table(project, table)
    fields = [field for column in columns if entity and (field := field_for_column(project, entity, column))]
    if not fields:
        return CheckResult(False, f"table {table} already has a row with {', '.join(columns)} {value}", "send a value that is not taken yet")
    field = fields[0]
    names = " and ".join(f"{entity.name}.{f.name}" for f in fields)
    repository = repository_for(project, entity)
    check = f"existsBy{field.name[:1].upper()}{field.name[1:]}({field.name})"
    return CheckResult(
        False,
        f"{names} must be unique ({unique_by(entity, field, columns[0])}), and a row with {field.name} {value} already exists",
        f"send a {field.name} that is not taken yet. To answer 409 Conflict instead of 500, check first with "
        f"{repository.name + '.' if repository else 'a repository method like '}{check}, "
        "or handle DataIntegrityViolationException in an @ExceptionHandler",
        entity.file,
        field.line,
    )


def open_in_view_note(project: SpringProject) -> str:
    setting = project.property("spring.jpa.open-in-view")
    if setting is None or setting.value != "false":
        return ""
    return (
        f"spring.jpa.open-in-view=false ({setting.file}:{setting.line}) closes the session when the repository call ends; "
        "the default, true, keeps it open until the response is written, but runs queries while writing it"
    )


def lazy_relationship_is_loaded(project: SpringProject, details: dict[str, str]) -> CheckResult:
    owner_name, _, field_name = details["role"].rpartition(".")
    owner = find_type(project, owner_name)
    field = next((f for f in owner.fields if f.name == field_name), None) if owner else None
    when = "while the response was written as JSON" if details.get("writing_json") else "in your code"
    label = f"{simple_type_name(owner_name)}.{field_name}"
    graph = f'@EntityGraph(attributePaths = "{field_name}")'
    repository = repository_for(project, owner) if owner else None
    load = f'load it with the query: {graph} on the {repository.name if repository else "repository"} method, or "join fetch" in a @Query'
    if details.get("writing_json"):
        fix = f"{load}; or return a DTO with only what the response needs; or, if the response does not need {field_name}, add @JsonIgnore to it"
    else:
        fix = f"read it inside a @Transactional service method, or {load}"
    if field is None:
        return CheckResult(False, f"{label} was read {when}, after the database session had closed", fix, note=open_in_view_note(project))
    relation = annotation_named(field.annotations, *RELATIONSHIPS)
    return CheckResult(
        False,
        f"{label} is a lazy {'@' + relation.name if relation else 'relationship'}, and it was read {when}, "
        "after the database session had closed",
        fix,
        owner.file,
        field.line,
        note=open_in_view_note(project),
    )


# --- the response: JSON that never ends ---


def type_names(type_text: str) -> list[str]:
    """Every type named in a declared type: List and Post for List<Post>."""
    return re.findall(r"[A-Za-z_$][\w$]*", type_text)


def is_written_as_json(java_type: JavaType, field: Field) -> bool:
    """Jackson writes a field with a public getter, a public field, or one marked @JsonProperty, unless it is ignored."""
    getter = next((m for m in java_type.methods if m.name == f"get{field.name[:1].upper()}{field.name[1:]}" and not m.parameters), None)
    if annotation_named(field.annotations, "JsonIgnore", "JsonBackReference"):
        return False
    if getter and annotation_named(getter.annotations, "JsonIgnore", "JsonBackReference"):
        return False
    return getter is not None or "public" in field.modifiers or bool(annotation_named(field.annotations, "JsonProperty"))


def links_written_as_json(project: SpringProject, entity: JavaType) -> list[tuple[Field, JavaType]]:
    """The relationship fields of `entity` that Jackson writes, with the entity each points at."""
    by_name = {e.name: e for e in entities(project)}
    return [
        (f, by_name[name])
        for f in entity.fields
        if annotation_named(f.annotations, *RELATIONSHIPS) and is_written_as_json(entity, f)
        for name in type_names(f.type)
        if name in by_name
    ]


def json_loop(project: SpringProject, start: list[JavaType]) -> tuple[JavaType, Field, JavaType, Field] | None:
    """Two fields that point at each other, both written as JSON: User.posts and Post.user."""
    for entity in start:
        for field, other in links_written_as_json(project, entity):
            back = next((f for f, target in links_written_as_json(project, other) if target is entity), None)
            if back is not None:
                return entity, field, other, back
    return None


def relation_text(project: SpringProject, java_type: JavaType, field: Field) -> str:
    relation = annotation_named(field.annotations, *RELATIONSHIPS)
    text = read_line(project, java_type.file, relation.line) if relation else None
    return text.strip() if text else ""


def json_fields_do_not_loop(project: SpringProject, details: dict[str, str]) -> CheckResult:
    found = handler_of(project, details)
    returned = [e for e in entities(project) if found and found[1].return_type and e.name in type_names(found[1].return_type)]
    loop = json_loop(project, returned) or json_loop(project, entities(project))
    if loop is None:
        return CheckResult(
            False, "no two entity fields point at each other: some object in the response contains itself", "return a DTO built for the response"
        )
    first, first_field, second, second_field = loop
    rows = [(first, first_field), (second, second_field)]
    labels = [f"{t.name}.{f.name}" for t, f in rows]
    relations = [relation_text(project, t, f) for t, f in rows]
    label_width, relation_width = max(map(len, labels)), max(map(len, relations))
    table = "\n".join(
        f"{label:<{label_width}}  {relation:<{relation_width}}  {t.file.name}:{f.line}"
        for label, relation, (t, f) in zip(labels, relations, rows)
    )
    # The link back is the @ManyToOne side, as a rule: the one to leave out of the JSON.
    owner, back = next(((t, f) for t, f in rows if annotation_named(f.annotations, "ManyToOne")), rows[1])
    parent, children = next((t, f) for t, f in rows if f is not back)
    return CheckResult(
        False,
        f"{labels[0]} and {labels[1]} point at each other, so the JSON goes "
        f"{first.name} → {first_field.name} → {second.name} → {second_field.name} → {first.name} → ... until Jackson stops:\n{table}",
        f"break the loop on one side: add @JsonIgnore to {owner.name}.{back.name} ({owner.file.name}:{back.line}), "
        f"or put @JsonManagedReference on {parent.name}.{children.name} and @JsonBackReference on {owner.name}.{back.name}, "
        "or return a DTO that leaves out the link back",
        owner.file,
        back.line,
        note=f"with Jackson 3 too, these annotations are imported from {JSON_ANNOTATIONS}, like {JSON_ANNOTATIONS}.JsonIgnore",
    )


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
