"""Read a Spring Boot project's .java files with regex, without a Java parser.

scan() reads every .java file under src/main/java and returns the types they
declare, and from those the beans, the places that ask for a bean, the
@SpringBootApplication class and every request mapping. It also reads
application.properties and application.yml (see properties.py).

Each file is looked at in two copies with the same offsets. In the first,
comments are blanked out, so a commented-out @Service does not count. In the
second, the inside of every string is blanked too, so structure (braces,
parentheses, commas) can be found without "/users/{id}" getting in the way.
Values such as mapping paths are then read from the first copy.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from itertools import product
from pathlib import Path

from fsd.scanner.properties import Property, read_properties

SOURCE_ROOT = Path("src") / "main" / "java"

# Class annotations that make Spring create a bean. @SpringBootApplication is a @Configuration too.
STEREOTYPES = {"Component", "Service", "Repository", "Controller", "RestController", "Configuration", "SpringBootApplication"}
# Spring Data interfaces: Spring creates a bean for every interface that extends one.
SPRING_DATA_REPOSITORIES = {
    "Repository",
    "CrudRepository",
    "ListCrudRepository",
    "PagingAndSortingRepository",
    "ListPagingAndSortingRepository",
    "JpaRepository",
}
INJECT = ("Autowired", "Inject")
# Lombok annotations that generate a constructor taking the final fields.
LOMBOK_REQUIRED_ARGS = ("RequiredArgsConstructor", "Data")
# Each mapping annotation and the HTTP method it fixes; @RequestMapping takes it from method=.
# What a bean is, by the annotation that made it one; any other bean is a component.
ROLES = {"@RestController": "controller", "@Controller": "controller", "@Service": "service", "@Repository": "repository"}
MAPPING_ANNOTATIONS = {
    "RequestMapping": None,
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}

LEXEMES = re.compile(
    r'(?P<comment>//[^\n]*|/\*[\s\S]*?(?:\*/|\Z))'
    r'|(?P<literal>"""[\s\S]*?(?:"""|\Z)|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')'
)
IDENTIFIER = r"[A-Za-z_$][\w$]*"
TYPE = rf"{IDENTIFIER}(?:\s*\.\s*{IDENTIFIER})*(?:\s*<[^()]*?>)?(?:\s*\[\s*\])*(?:\s*\.\.\.)?"
PACKAGE = re.compile(rf"\bpackage\s+({IDENTIFIER}(?:\s*\.\s*{IDENTIFIER})*)\s*;")
IMPORT = re.compile(rf"import\s+(?P<static>static\s+)?(?P<name>{IDENTIFIER}(?:\s*\.\s*(?:{IDENTIFIER}|\*))*)\s*$")
ANNOTATION = re.compile(rf"@(?!interface\b)\s*(?P<name>{IDENTIFIER}(?:\s*\.\s*{IDENTIFIER})*)")
MODIFIER = re.compile(
    r"(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp"
    r"|default|synchronized|native|transient|volatile)\b"
)
TYPE_DECLARATION = re.compile(rf"(?P<kind>class|interface|enum|record|@interface)\s+(?P<name>{IDENTIFIER})")
SUPERTYPE_KEYWORD = re.compile(r"\b(extends|implements|permits)\b")
METHOD = re.compile(
    rf"(?:<.*?>\s*)?(?:(?P<type>{TYPE})\s+)?(?P<name>{IDENTIFIER})\s*(?P<params>\(.*\))"
    r"\s*(?:\[\s*\]\s*)*(?:throws\s+[\w$.,\s<>]*)?$",
    re.DOTALL,
)
FIELD = re.compile(rf"(?P<type>{TYPE})\s+(?P<names>{IDENTIFIER}(?:\s*\[\s*\])*(?:\s*,\s*{IDENTIFIER}(?:\s*\[\s*\])*)*)\s*$")
PARAMETER = re.compile(rf"(?P<type>{TYPE})\s+(?P<name>{IDENTIFIER})(?:\s*\[\s*\])*\s*$")
ATTRIBUTE = re.compile(rf"\s*(?P<name>{IDENTIFIER})\s*=(?!=)")
WHITESPACE = re.compile(r"\s*")
STRUCTURE = re.compile(r"[(){};]")
BRACKET_OR_PUNCTUATION = re.compile(r"[()\[\]{}<>,=;]")
BRACKETS = {opening: re.compile(re.escape(opening) + "|" + re.escape(closing)) for opening, closing in ("()", "[]", "{}", "<>")}
STRING_LITERAL = re.compile(r'"((?:\\.|[^"\\])*)"')
JOINED_STRINGS = re.compile(r'\s*"(?:\\.|[^"\\])*"(?:\s*\+\s*"(?:\\.|[^"\\])*")*\s*')
ESCAPE = re.compile(r"\\(.)")
ESCAPED = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "s": " ", "0": "\0"}


@dataclass
class Annotation:
    name: str  # without @ or package: GetMapping
    arguments: dict[str, list[str]]  # each attribute's values as written in Java; a lone value is "value"
    line: int

    def values(self, *names: str) -> list[str]:
        """The values of the first attribute in `names` that is set, e.g. values("value", "path")."""
        for name in names:
            if name in self.arguments:
                return self.arguments[name]
        return []


@dataclass
class Parameter:
    type: str  # as written: UserService, List<Plugin>
    name: str
    annotations: list[Annotation]
    line: int


@dataclass
class Field:
    type: str
    name: str
    annotations: list[Annotation]
    line: int
    modifiers: list[str] = field(default_factory=list)  # final, static, ...
    initialized: bool = False  # declared with = value


@dataclass
class Import:
    name: str  # jakarta.persistence.Entity, or jakarta.persistence.* for a wildcard
    line: int
    static: bool = False


@dataclass
class Method:
    name: str
    return_type: str | None  # None for a constructor
    parameters: list[Parameter]
    annotations: list[Annotation]
    line: int


@dataclass
class JavaType:
    kind: str  # class, interface, record or enum
    name: str
    package: str
    file: Path  # relative to the project folder
    line: int  # the line with the class, interface, record or enum keyword
    annotations: list[Annotation] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)  # as written: JpaRepository<User, Long>
    implements: list[str] = field(default_factory=list)
    enclosing: str = ""  # the outer class names of a nested type: AppConfig
    imports: list[Import] = field(default_factory=list)  # the imports of its file
    fields: list[Field] = field(default_factory=list)
    constructors: list[Method] = field(default_factory=list)
    methods: list[Method] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return ".".join(part for part in (self.package, self.enclosing, self.name) if part)


@dataclass
class Bean:
    name: str  # Spring's bean name: userService
    type: str  # the class or interface, or the @Bean method's return type as written
    reason: str  # why Spring creates it: "@Service", "extends JpaRepository", "@Bean method"
    declared_in: JavaType
    file: Path
    line: int


@dataclass
class InjectionPoint:
    owner: JavaType
    type: str  # the type asked for, as written
    name: str  # the parameter or field name
    where: str  # worded like Spring's errors: "Parameter 0 of constructor", "Field userService"
    file: Path
    line: int


@dataclass
class RequestMapping:
    http_method: str | None  # GET, POST, ...; None when any method matches
    path: str  # the full path: class-level @RequestMapping plus the method's own path
    controller: JavaType
    method: str  # the handler method's name
    file: Path
    line: int  # the line of the mapping annotation on the method
    own_path: str = ""  # the method's own path, as written: /{id}
    base_path: str = ""  # the class-level @RequestMapping path, as written: /users

    @property
    def handler(self) -> str:
        return f"{self.controller.name}#{self.method}"


@dataclass
class SpringProject:
    root: Path
    types: list[JavaType]
    beans: list[Bean]
    injection_points: list[InjectionPoint]
    application: JavaType | None  # the @SpringBootApplication class
    mappings: list[RequestMapping]
    properties: list[Property] = field(default_factory=list)  # application.properties, then application.yml

    def types_named(self, name: str) -> list[JavaType]:
        """Types whose simple or full name is `name`."""
        return [t for t in self.types if name in (t.name, t.full_name)]

    def type_of_class(self, class_name: str) -> JavaType | None:
        """The type a stack frame's class belongs to: UserService for UserService$$SpringCGLIB$$0 or UserService$1."""
        base = class_name.split("$$")[0]
        for name in (base.replace("$", "."), base.split("$")[0]):
            if found := self.types_named(name):
                return found[0]
        return None

    def role(self, class_name: str) -> str | None:
        """controller, service, repository or component for a bean, class for other types of the app; None outside it."""
        java_type = self.type_of_class(class_name)
        if java_type is None:
            return None
        bean = next((b for b in self.beans if b.declared_in is java_type and b.reason != "@Bean method"), None)
        if bean is None:
            return "class"
        if bean.reason.startswith("extends"):
            return "repository"
        return ROLES.get(bean.reason, "component")

    def property(self, key: str) -> Property | None:
        """The value Spring Boot uses for `key`: application.properties wins over application.yml."""
        return next((p for p in self.properties if p.key == key), None)


# --- reading one file ---


def blank(text: str) -> str:
    """Spaces in place of `text`, keeping its line breaks so line numbers stay right."""
    return re.sub(r"[^\n]", " ", text)


def mask(text: str) -> tuple[str, str]:
    """Return (source, code): source has comments blanked, code also has the inside of every literal blanked."""
    source, code = [], []
    last = 0
    for found in LEXEMES.finditer(text):
        between = text[last : found.start()]
        literal = found[0]
        if found.lastgroup == "comment":
            source += [between, blank(literal)]
            code += [between, blank(literal)]
        else:
            source += [between, literal]
            code += [between, literal[0] + blank(literal[1:-1]) + literal[-1]]
        last = found.end()
    return "".join(source + [text[last:]]), "".join(code + [text[last:]])


def normalize(text: str) -> str:
    return " ".join(text.split())


class JavaFile:
    """Find the types declared in one .java file, with their annotations and members."""

    def __init__(self, text: str, path: Path):
        self.source, self.code = mask(text)
        self.path = path
        self.line_starts = [0] + [found.end() for found in re.finditer("\n", text)]
        package = PACKAGE.search(self.code)
        self.package = re.sub(r"\s", "", package[1]) if package else ""
        self.types: list[JavaType] = []
        self.imports: list[Import] = []
        self.read_members(0, len(self.code), owner=None)

    def line(self, pos: int) -> int:
        return bisect_right(self.line_starts, pos)

    def skip_space(self, pos: int, end: int) -> int:
        return WHITESPACE.match(self.code, pos, end).end()

    def matching(self, open_pos: int) -> int:
        """The position of the bracket that closes the one at open_pos, or the end of the file."""
        opening = self.code[open_pos]
        depth = 0
        for found in BRACKETS[opening].finditer(self.code, open_pos):
            depth += 1 if found[0] == opening else -1
            if depth == 0:
                return found.start()
        return len(self.code) - 1

    def top_level(self, start: int, end: int, wanted: str, angles: bool = True) -> list[int]:
        """Positions of the `wanted` characters in code[start:end] that are not inside brackets."""
        openings, closings = ("([{<", ")]}>") if angles else ("([{", ")]}")
        positions, depth = [], 0
        for found in BRACKET_OR_PUNCTUATION.finditer(self.code, start, end):
            char = found[0]
            if char in openings:
                depth += 1
            elif char in closings:
                depth = max(depth - 1, 0)
            elif depth == 0 and char in wanted:
                positions.append(found.start())
        return positions

    def split_commas(self, start: int, end: int) -> list[tuple[int, int]]:
        cuts = self.top_level(start, end, ",")
        return list(zip([start] + [cut + 1 for cut in cuts], cuts + [end]))

    def split_members(self, start: int, end: int):
        """Split code[start:end] into the declarations at this level.

        Yields (start, head_end, end) for each one. Its head runs up to the {
        of its body, or up to its ;.
        """
        chunk = pos = start
        parens = 0
        while found := STRUCTURE.search(self.code, pos, end):
            char, pos = found[0], found.end()
            if char == "(":
                parens += 1
            elif char == ")":
                parens = max(parens - 1, 0)
            elif parens == 0 and char == ";":
                yield chunk, found.start(), pos
                chunk = pos
            elif parens == 0 and char == "{":
                pos = min(self.matching(found.start()) + 1, end)
                yield chunk, found.start(), pos
                chunk = pos

    def read_members(self, start: int, end: int, owner: JavaType | None) -> None:
        for chunk_start, head_end, chunk_end in self.split_members(start, end):
            self.read_member(chunk_start, head_end, chunk_end, owner)

    def read_member(self, start: int, head_end: int, end: int, owner: JavaType | None) -> None:
        annotations, modifiers, pos = self.read_prefix(start, head_end)
        declared = TYPE_DECLARATION.match(self.code, pos, head_end)
        if declared:
            self.read_type(declared, annotations, head_end, end, owner)
        elif owner is None:
            self.read_import(pos, head_end)
        elif initializer := self.top_level(pos, head_end, "="):
            self.read_field(owner, Field("", "", annotations, self.line(pos), modifiers, initialized=True), pos, initializer[0])
        elif method := METHOD.match(self.code, pos, head_end):
            self.read_method(owner, annotations, method, pos)
        else:
            self.read_field(owner, Field("", "", annotations, self.line(pos), modifiers), pos, head_end)

    def read_import(self, pos: int, end: int) -> None:
        if found := IMPORT.match(self.code, pos, end):
            self.imports.append(Import(re.sub(r"\s", "", found["name"]), self.line(pos), bool(found["static"])))

    def read_prefix(self, pos: int, end: int) -> tuple[list[Annotation], list[str], int]:
        """Read the annotations and modifiers at `pos`; return them and where the declaration starts."""
        annotations, modifiers = [], []
        while True:
            pos = self.skip_space(pos, end)
            if found := ANNOTATION.match(self.code, pos, end):
                annotation, pos = self.read_annotation(found, end)
                annotations.append(annotation)
            elif found := MODIFIER.match(self.code, pos, end):
                modifiers.append(found[0])
                pos = found.end()
            else:
                return annotations, modifiers, pos

    def read_annotation(self, found: re.Match, end: int) -> tuple[Annotation, int]:
        name = re.sub(r"\s", "", found["name"]).split(".")[-1]
        pos = self.skip_space(found.end(), end)
        arguments = {}
        if self.code.startswith("(", pos):
            close = self.matching(pos)
            arguments = self.read_arguments(pos + 1, close)
            pos = close + 1
        return Annotation(name, arguments, self.line(found.start())), pos

    def read_arguments(self, start: int, end: int) -> dict[str, list[str]]:
        arguments = {}
        for part_start, part_end in self.split_commas(start, end):
            named = ATTRIBUTE.match(self.code, part_start, part_end)
            name, value_start = (named["name"], named.end()) if named else ("value", part_start)
            if values := self.read_values(value_start, part_end):
                arguments[name] = values
        return arguments

    def read_values(self, start: int, end: int) -> list[str]:
        """One value, or each value of an array like {"/a", "/b"}, as written in the source."""
        start = self.skip_space(start, end)
        parts = [(start, end)]
        if self.code.startswith("{", start):
            parts = self.split_commas(start + 1, self.matching(start))
        return [text for part_start, part_end in parts if (text := self.source[part_start:part_end].strip())]

    def read_type(self, declared: re.Match, annotations: list[Annotation], head_end: int, end: int, owner) -> None:
        if declared["kind"] == "@interface":
            return
        java_type = JavaType(
            kind=declared["kind"],
            name=declared["name"],
            package=self.package,
            file=self.path,
            line=self.line(declared.start()),
            annotations=annotations,
            enclosing=f"{owner.enclosing}.{owner.name}".lstrip(".") if owner else "",
            imports=self.imports,
        )
        pos = self.skip_space(declared.end(), head_end)
        if self.code.startswith("<", pos):
            pos = self.matching(pos) + 1
        if java_type.kind == "record":
            pos = self.read_record_components(java_type, pos, head_end)
        self.read_supertypes(java_type, pos, head_end)
        self.types.append(java_type)
        if self.code[head_end] == "{":
            body_start = head_end + 1
            if java_type.kind == "enum":
                body_start = self.after_enum_constants(body_start, end - 1)
            self.read_members(body_start, end - 1, java_type)
        if constructor := lombok_constructor(java_type):
            java_type.constructors.append(constructor)

    def read_record_components(self, record: JavaType, pos: int, end: int) -> int:
        """Read a record's components as its fields and canonical constructor; return where they end."""
        pos = self.skip_space(pos, end)
        if not self.code.startswith("(", pos):
            return pos
        close = self.matching(pos)
        components = self.read_parameters(pos + 1, close)
        record.fields += [Field(c.type, c.name, c.annotations, c.line) for c in components]
        record.constructors.append(Method(record.name, None, components, [], record.line))
        return close + 1

    def read_supertypes(self, java_type: JavaType, start: int, end: int) -> None:
        keywords = [
            keyword
            for keyword in SUPERTYPE_KEYWORD.finditer(self.code, start, end)
            if self.code.count("<", start, keyword.start()) == self.code.count(">", start, keyword.start())
        ]
        for keyword, following in zip(keywords, keywords[1:] + [None]):
            types_end = following.start() if following else end
            names = [normalize(self.code[s:e]) for s, e in self.split_commas(keyword.end(), types_end)]
            if keyword[1] == "extends":
                java_type.extends += [name for name in names if name]
            elif keyword[1] == "implements":
                java_type.implements += [name for name in names if name]

    def after_enum_constants(self, start: int, end: int) -> int:
        constants_end = self.top_level(start, end, ";", angles=False)
        return constants_end[0] + 1 if constants_end else end

    def read_method(self, owner: JavaType, annotations: list[Annotation], found: re.Match, pos: int) -> None:
        parameters = self.read_parameters(found.start("params") + 1, found.end("params") - 1)
        return_type = normalize(found["type"]) if found["type"] else None
        method = Method(found["name"], return_type, parameters, annotations, self.line(pos))
        if return_type is not None:
            owner.methods.append(method)
        elif method.name == owner.name:
            owner.constructors.append(method)

    def read_field(self, owner: JavaType, declared: Field, pos: int, end: int) -> None:
        """Read `Type name` or `Type a, b` into fields like `declared`, which has the rest filled in."""
        found = FIELD.match(self.code, pos, end)
        if found:
            for name in re.findall(IDENTIFIER, found["names"]):
                owner.fields.append(replace(declared, type=normalize(found["type"]), name=name))

    def read_parameters(self, start: int, end: int) -> list[Parameter]:
        parameters = []
        for part_start, part_end in self.split_commas(start, end):
            annotations, _, pos = self.read_prefix(part_start, part_end)
            found = PARAMETER.match(self.code, pos, part_end)
            if found:
                parameters.append(Parameter(normalize(found["type"]), found["name"], annotations, self.line(pos)))
        return parameters


# --- what the types mean to Spring ---


def annotation_named(annotations: list[Annotation], *names: str) -> Annotation | None:
    return next((annotation for annotation in annotations if annotation.name in names), None)


def simple_type_name(type_text: str) -> str:
    """UserService from com.demo.UserService, JpaRepository from JpaRepository<User, Long>."""
    erased = re.sub(r"<.*>|\[\s*\]|\.\.\.", "", type_text)
    return erased.split(".")[-1].strip()


def unescape(text: str) -> str:
    return ESCAPE.sub(lambda found: ESCAPED.get(found[1], found[1]), text)


def java_string(value: str) -> str | None:
    """The text of a string literal, or of literals joined with +; None for anything else."""
    if not JOINED_STRINGS.fullmatch(value):
        return None
    return "".join(unescape(text) for text in STRING_LITERAL.findall(value))


def first_string(values: list[str]) -> str | None:
    return next((text for value in values if (text := java_string(value))), None)


def decapitalize(name: str) -> str:
    """Spring's default bean name: UserService -> userService, but URLParser stays URLParser."""
    if len(name) > 1 and name[0].isupper() and name[1].isupper():
        return name
    return name[:1].lower() + name[1:]


def is_lombok_required(f: Field) -> bool:
    """A field @RequiredArgsConstructor takes: final or @NonNull, and not already given a value."""
    return "static" not in f.modifiers and not f.initialized and ("final" in f.modifiers or bool(annotation_named(f.annotations, "NonNull")))


def is_lombok_all(f: Field) -> bool:
    """A field @AllArgsConstructor takes: every instance field, except a final one already given a value."""
    return "static" not in f.modifiers and not ("final" in f.modifiers and f.initialized)


def lombok_constructor(java_type: JavaType) -> Method | None:
    """The constructor Lombok generates, with the fields as parameters in declaration order."""
    if annotation := annotation_named(java_type.annotations, "AllArgsConstructor"):
        fields = [f for f in java_type.fields if is_lombok_all(f)]
    elif annotation := annotation_named(java_type.annotations, *LOMBOK_REQUIRED_ARGS):
        if annotation.name == "Data" and java_type.constructors:
            return None  # @Data only adds a constructor when the class has none
        fields = [f for f in java_type.fields if is_lombok_required(f)]
    else:
        return None
    parameters = [Parameter(f.type, f.name, f.annotations, f.line) for f in fields]
    return Method(java_type.name, None, parameters, [], annotation.line)


def component_annotation(java_type: JavaType) -> Annotation | None:
    if java_type.kind not in ("class", "record"):
        return None
    return annotation_named(java_type.annotations, *STEREOTYPES)


def spring_data_base(interface: JavaType, interfaces: dict[str, JavaType], seen: set[str]) -> str | None:
    """The Spring Data interface that `interface` extends, directly or through other interfaces."""
    seen.add(interface.name)
    for parent in interface.extends:
        name = simple_type_name(parent)
        if name in SPRING_DATA_REPOSITORIES:
            return name
        if name in interfaces and name not in seen:
            if base := spring_data_base(interfaces[name], interfaces, seen):
                return base
    return None


def repository_base(java_type: JavaType, interfaces: dict[str, JavaType]) -> str | None:
    """The Spring Data interface a repository bean extends; None if it is not one."""
    if java_type.kind != "interface" or annotation_named(java_type.annotations, "NoRepositoryBean"):
        return None
    return spring_data_base(java_type, interfaces, set())


def type_bean(java_type: JavaType, interfaces: dict[str, JavaType]) -> Bean | None:
    if stereotype := component_annotation(java_type):
        name = first_string(stereotype.values("value")) or decapitalize(java_type.name)
        return Bean(name, java_type.name, f"@{stereotype.name}", java_type, java_type.file, java_type.line)
    if base := repository_base(java_type, interfaces):
        return Bean(decapitalize(java_type.name), java_type.name, f"extends {base}", java_type, java_type.file, java_type.line)
    return None


def bean_method_beans(java_type: JavaType) -> list[Bean]:
    beans = []
    for method in java_type.methods:
        if bean := annotation_named(method.annotations, "Bean"):
            name = first_string(bean.values("name", "value")) or method.name
            beans.append(Bean(name, method.return_type, "@Bean method", java_type, java_type.file, method.line))
    return beans


def find_beans(types: list[JavaType]) -> list[Bean]:
    interfaces = {t.name: t for t in types if t.kind == "interface"}
    beans = []
    for java_type in types:
        if bean := type_bean(java_type, interfaces):
            beans.append(bean)
        beans += bean_method_beans(java_type)
    return beans


def asks_for_bean(member: Parameter | Field) -> bool:
    """@Value asks for a property, not a bean."""
    return annotation_named(member.annotations, "Value") is None


def injected_constructor(java_type: JavaType) -> Method | None:
    """The constructor Spring calls: the only one, or the one marked @Autowired."""
    if len(java_type.constructors) == 1:
        return java_type.constructors[0]
    return next((c for c in java_type.constructors if annotation_named(c.annotations, *INJECT)), None)


def parameter_injection(java_type: JavaType, method: Method, where: str) -> list[InjectionPoint]:
    return [
        InjectionPoint(java_type, parameter.type, parameter.name, f"Parameter {index} of {where}", java_type.file, parameter.line)
        for index, parameter in enumerate(method.parameters)
        if asks_for_bean(parameter)
    ]


def constructor_injection(java_type: JavaType) -> list[InjectionPoint]:
    constructor = injected_constructor(java_type)
    return parameter_injection(java_type, constructor, "constructor") if constructor else []


def field_injection(java_type: JavaType) -> list[InjectionPoint]:
    return [
        InjectionPoint(java_type, f.type, f.name, f"Field {f.name}", java_type.file, f.line)
        for f in java_type.fields
        if annotation_named(f.annotations, *INJECT) and asks_for_bean(f)
    ]


def bean_method_injection(java_type: JavaType) -> list[InjectionPoint]:
    points = []
    for method in java_type.methods:
        if annotation_named(method.annotations, "Bean"):
            points += parameter_injection(java_type, method, f"method {method.name}")
    return points


def find_injection_points(types: list[JavaType]) -> list[InjectionPoint]:
    """Where beans ask Spring for other beans: constructors, @Autowired fields and @Bean method parameters."""
    points = []
    for java_type in types:
        if component_annotation(java_type):
            points += constructor_injection(java_type) + field_injection(java_type)
        points += bean_method_injection(java_type)
    return points


def find_application(types: list[JavaType]) -> JavaType | None:
    return next((t for t in types if annotation_named(t.annotations, "SpringBootApplication")), None)


def path_text(value: str) -> str:
    """A mapping path; a constant this scanner cannot resolve is kept as <Paths.USERS>."""
    text = java_string(value)
    return text if text is not None else f"<{value}>"


def mapping_paths(annotation: Annotation) -> list[str]:
    return [path_text(value) for value in annotation.values("value", "path")] or [""]


def mapping_methods(annotation: Annotation) -> list[str]:
    if fixed := MAPPING_ANNOTATIONS.get(annotation.name):
        return [fixed]
    return [value.split(".")[-1].strip() for value in annotation.values("method")]


def with_leading_slash(path: str) -> str:
    return path if not path or path.startswith("/") else "/" + path


def join_paths(base: str, path: str) -> str:
    """Join a class-level and a method-level path as Spring does: /users + /{id} = /users/{id}."""
    base, path = with_leading_slash(base), with_leading_slash(path)
    if base.endswith("/") and path.startswith("/"):
        base = base[:-1]
    return base + path or "/"


def method_mappings(
    controller: JavaType, method: Method, annotation: Annotation, class_level: Annotation | None
) -> list[RequestMapping]:
    base_paths = mapping_paths(class_level) if class_level else [""]
    http_methods = mapping_methods(annotation) + (mapping_methods(class_level) if class_level else [])
    return [
        RequestMapping(http_method, join_paths(base, path), controller, method.name, controller.file, annotation.line, path, base)
        for base, path, http_method in product(base_paths, mapping_paths(annotation), list(dict.fromkeys(http_methods)) or [None])
    ]


def find_mappings(types: list[JavaType]) -> list[RequestMapping]:
    mappings = []
    for java_type in types:
        if java_type.kind == "interface":
            continue  # e.g. HTTP client interfaces; Spring MVC serves classes
        class_level = annotation_named(java_type.annotations, "RequestMapping")
        for method in java_type.methods:
            for annotation in method.annotations:
                if annotation.name in MAPPING_ANNOTATIONS:
                    mappings += method_mappings(java_type, method, annotation, class_level)
    return mappings


def scan(root: Path | str = ".") -> SpringProject:
    """Read every .java file under <root>/src/main/java, and the application properties."""
    root = Path(root).resolve()
    types = []
    for path in sorted((root / SOURCE_ROOT).rglob("*.java")):
        types += JavaFile(path.read_text(encoding="utf-8", errors="replace"), path.relative_to(root)).types
    return SpringProject(
        root=root,
        types=types,
        beans=find_beans(types),
        injection_points=find_injection_points(types),
        application=find_application(types),
        mappings=find_mappings(types),
        properties=read_properties(root),
    )
