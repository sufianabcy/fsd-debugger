"""Read the dependencies in pom.xml or build.gradle(.kts), keeping the file and line of each.

Plain regex, like the rest of the scanner: enough to tell which starters and
drivers a project uses, not a Maven or Gradle model. Versions, properties,
parent POMs and Gradle version catalogs (libs.xyz) are not resolved.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")

XML_COMMENT = re.compile(r"<!--[\s\S]*?(?:-->|\Z)")
# Dependencies that are not the app's own: version management, and what plugins need.
NOT_APP_DEPENDENCIES = re.compile(r"<(dependencyManagement|plugin)>[\s\S]*?</\1>")
MAVEN_DEPENDENCY = re.compile(r"<dependency>(?P<body>[\s\S]*?)</dependency>")
MAVEN_FIELD = re.compile(r"<(?P<name>groupId|artifactId|scope)>\s*(?P<value>[^<]*?)\s*</(?P=name)>")
GRADLE_COMMENT = re.compile(r"//[^\n]*|/\*[\s\S]*?(?:\*/|\Z)")
# implementation 'g:a:1.0', runtimeOnly("g:a"), testImplementation group: 'g', name: 'a'
GRADLE_DEPENDENCY = re.compile(
    r"^\s*(?P<scope>\w+)\s*\(?\s*(?:"
    r"['\"](?P<group>[^:'\"\s]+):(?P<artifact>[^:'\"\s]+)[^'\"]*['\"]"
    r"|group\s*[:=]\s*['\"](?P<named_group>[^'\"]+)['\"]\s*,\s*name\s*[:=]\s*['\"](?P<named_artifact>[^'\"]+)['\"])",
    re.MULTILINE,
)


@dataclass
class Dependency:
    group: str
    artifact: str
    scope: str  # Maven's scope (compile when not set) or Gradle's configuration: runtimeOnly, testImplementation, ...
    file: Path  # relative to the project folder
    line: int

    @property
    def for_tests_only(self) -> bool:
        return self.scope == "test" or self.scope.startswith("test")


def blank(text: str) -> str:
    """Spaces in place of `text`, keeping its line breaks so line numbers stay right."""
    return re.sub(r"[^\n]", " ", text)


def line_of(text: str, pos: int) -> int:
    return bisect_right([found.end() for found in re.finditer("\n", text)], pos) + 1


def maven_dependencies(text: str, file: Path) -> list[Dependency]:
    text = XML_COMMENT.sub(lambda found: blank(found[0]), text)
    text = NOT_APP_DEPENDENCIES.sub(lambda found: blank(found[0]), text)
    found = []
    for dependency in MAVEN_DEPENDENCY.finditer(text):
        fields = {f["name"]: f["value"] for f in MAVEN_FIELD.finditer(dependency["body"])}
        if "artifactId" in fields:
            line = line_of(text, dependency.start())
            found.append(Dependency(fields.get("groupId", ""), fields["artifactId"], fields.get("scope", "compile"), file, line))
    return found


def gradle_dependencies(text: str, file: Path) -> list[Dependency]:
    text = GRADLE_COMMENT.sub(lambda found: blank(found[0]), text)
    return [
        Dependency(
            found["group"] or found["named_group"],
            found["artifact"] or found["named_artifact"],
            found["scope"],
            file,
            line_of(text, found.start("scope")),
        )
        for found in GRADLE_DEPENDENCY.finditer(text)
    ]


def build_file(root: Path) -> Path | None:
    """The project's pom.xml or build.gradle(.kts), relative to `root`; None if it has none."""
    return next((Path(name) for name in BUILD_FILES if (root / name).is_file()), None)


def read_dependencies(root: Path) -> list[Dependency]:
    """The dependencies in <root>/pom.xml, build.gradle or build.gradle.kts."""
    file = build_file(root)
    if file is None:
        return []
    text = (root / file).read_text(encoding="utf-8", errors="replace")
    read = maven_dependencies if file.name == "pom.xml" else gradle_dependencies
    return read(text, file)
