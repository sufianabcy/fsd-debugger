"""What the startup and request checks share: the result of a check, and reading the project for one.

Each check takes the scanned project and the failure's details from the log,
and returns a CheckResult, or None when it does not apply (say, an annotation
check on an interface).
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fsd.scanner.spring import Bean, JavaType, SpringProject

# Where a property can be fixed, by the type Spring could not convert it to: (what it needs, an example).
EXPECTED_VALUES = [
    ({"Integer", "int", "Long", "long", "Short", "short", "Byte", "byte"}, "a whole number", "10"),
    ({"Boolean", "boolean"}, "true or false", "true"),
    ({"Double", "double", "Float", "float", "BigDecimal"}, "a number", "1.5"),
    ({"Duration"}, "a duration", "30s"),
    ({"DataSize"}, "a size", "10MB"),
]


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
        return f"{self.file.as_posix()}:{self.line}" if self.line else self.file.as_posix()


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


def is_scanned_fix(java_type: JavaType, app: JavaType, how: str) -> str:
    return f"move {java_type.name} into {app.package} or a package below it, or {how}"


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


# --- wording ---


def joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def expected_value(key: str, target_type: str) -> tuple[str, str]:
    """What `target_type` needs, in words, and an example value for `key`."""
    for types, needs, example in EXPECTED_VALUES:
        if target_type in types:
            return needs, "8080" if key.endswith("port") and needs == "a whole number" else example
    return f"a valid {target_type}", ""
