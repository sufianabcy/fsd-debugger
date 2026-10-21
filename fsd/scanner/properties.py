"""Read application.properties and application.yml, keeping the file and line of each key."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

RESOURCES = Path("src") / "main" / "resources"
# In the order Spring Boot prefers them: .properties wins over .yml.
PROPERTY_FILES = ("application.properties", "application.yml", "application.yaml")

KEY_VALUE = re.compile(r"(?P<key>(?:\\.|[^\\=:\s])+)[ \t\f]*[=:]?[ \t\f]*(?P<value>.*)")
ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)")
ESCAPED = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}


@dataclass
class Property:
    key: str  # server.port, or a list item like spring.profiles.include[0]
    value: str
    file: Path  # relative to the project folder
    line: int


def unescape(text: str) -> str:
    def replace(found: re.Match) -> str:
        code = found[1]
        if len(code) == 5:
            return chr(int(code[1:], 16))
        return ESCAPED.get(code, code)

    return ESCAPE.sub(replace, text)


def continues(line: str) -> bool:
    """A line ending in an odd number of backslashes continues on the next one."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def properties_file(text: str, file: Path) -> list[Property]:
    found = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        start = index
        line = lines[index].lstrip()
        index += 1
        if not line or line[0] in "#!":
            continue
        while continues(line) and index < len(lines):
            line = line[:-1] + lines[index].lstrip()
            index += 1
        if pair := KEY_VALUE.match(line):
            found.append(Property(unescape(pair["key"]), unescape(pair["value"]).rstrip(), file, start + 1))
    return found


def yaml_node(node: yaml.Node, prefix: str, file: Path, found: list[Property]) -> None:
    if isinstance(node, yaml.MappingNode):
        children = [(f"{prefix}.{key.value}" if prefix else str(key.value), key, value) for key, value in node.value]
    elif isinstance(node, yaml.SequenceNode):
        children = [(f"{prefix}[{index}]", item, item) for index, item in enumerate(node.value)]
    else:
        return
    for key, key_node, value in children:
        if isinstance(value, yaml.ScalarNode):
            found.append(Property(key, value.value, file, key_node.start_mark.line + 1))
        else:
            yaml_node(value, key, file, found)


def yaml_file(text: str, file: Path) -> list[Property]:
    found = []
    try:
        for document in yaml.compose_all(text):
            if document is not None:
                yaml_node(document, "", file, found)
    except yaml.YAMLError:
        pass  # a broken file is its own startup failure; nothing to read from it here
    return found


def read_properties(root: Path) -> list[Property]:
    """Every key in <root>/src/main/resources/application.properties, .yml and .yaml."""
    found = []
    for name in PROPERTY_FILES:
        path = root / RESOURCES / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            read = properties_file if name.endswith(".properties") else yaml_file
            found += read(text, path.relative_to(root))
    return found
