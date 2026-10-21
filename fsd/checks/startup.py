"""Startup checklists: the causes of a failure before the app is ready, looked for in its source."""

import re
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from fsd.checks.common import (
    CheckResult,
    add_annotation_fix,
    declared_bean,
    expected_value,
    find_type,
    is_under,
    joined,
    package_is_scanned,
    read_line,
)
from fsd.scanner.properties import RESOURCES
from fsd.scanner.spring import JavaType, SpringProject, annotation_named, simple_type_name

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
DATABASE_NAMES = {"h2": "the H2 server", "mysql": "MySQL", "mariadb": "MariaDB", "postgresql": "PostgreSQL",
                  "sqlserver": "SQL Server", "oracle": "Oracle"}  # fmt: skip
JDBC_URL = re.compile(r"jdbc:(?P<kind>\w+)(?::(?P<mode>tcp|ssl))?.*?//(?P<host>[^/:;?]+)(?::(?P<port>\d+))?")
SYMBOL_OWNER = re.compile(r"(?:of type|^(?:class|interface|enum|record))\s+(?P<type>[\w.$]+)")
HANDLER = re.compile(r"(?P<owner>[\w.$]+)#(?P<method>\w+)\(")
LOGGED_FILE = re.compile(r"\[(?P<name>[^\]]+)\]")


# --- missing bean: "required a bean of type 'X' that could not be found" ---


def suggested_bean_annotation(class_name: str) -> tuple[str, str]:
    """@Service for UserService or UserServiceImpl, @Repository for UserDao, and so on."""
    base = class_name.removesuffix("Impl")
    for endings, annotation, import_name in BEAN_ANNOTATIONS:
        if base.endswith(endings):
            return annotation, import_name
    return COMPONENT


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
    rows = [f"{label:<{width}}  {file.as_posix()}:{line}" if file else label for label, file, line in handlers]
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
        return f"change server.port in {current.file.as_posix()}:{current.line} to a free port, like {port + 1}"
    properties = next((p.file for p in project.properties), RESOURCES / "application.properties")
    return f"run this app on another port: add server.port={port + 1} to {properties.as_posix()}"


def port_is_used_by(project: SpringProject, port: int, listener: Listener | None) -> CheckResult:
    """Port `port` is taken: by `listener`, or by a program the OS would not name when it is None."""
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
