"""Compare a requested path with the paths an app maps, the way Spring matches them.

A mapped path is a template: {id} matches any one segment, {id:\\d+} one that
fits the regex, * and ? part of a segment, and /** or /{*rest} everything
after. Everything here is plain string work on the two paths.
"""

import re
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import product
from urllib.parse import parse_qsl, urlencode

# Words people put in a path for one another; tried before spelling distance.
SYNONYMS = [("all", "everything"), ("find", "get"), ("remove", "delete"), ("user", "users")]

TEMPLATE_PART = re.compile(r"/\*\*$|/\{\*\w*\}$|\{\w*(?::(?P<regex>(?:[^{}]|\{[^{}]*\})+))?\}|\*|\?")
WORD = re.compile(r"[A-Za-z][a-z]*")


def without_query(path: str) -> str:
    return path.split("?", 1)[0]


def query_value(path: str, name: str) -> str | None:
    return next((value for key, value in parse_qsl(path.partition("?")[2], keep_blank_values=True) if key == name), None)


def with_query_value(path: str, name: str, value: str) -> str:
    """`path` with ?name=value set, keeping its other query parameters: /a?x=1&limit=10 for /a?x=1&limit=ten."""
    base, _, query = path.partition("?")
    pairs = parse_qsl(query, keep_blank_values=True)
    if any(key == name for key, _ in pairs):
        pairs = [(key, value if key == name else old) for key, old in pairs]
    else:
        pairs.append((name, value))
    return f"{base}?{urlencode(pairs)}"


def template_pattern(template: str, keep_regex: bool) -> str:
    pattern, last = "", 0
    for found in TEMPLATE_PART.finditer(template):
        token = found[0]
        if token.startswith("/"):
            part = "(?:/.*)?"
        elif token in ("*", "?"):
            part = "[^/]*" if token == "*" else "[^/]"
        elif found["regex"] and keep_regex:
            part = f"(?:{found['regex']})"
        else:
            part = "[^/]+"
        pattern += re.escape(template[last : found.start()]) + part
        last = found.end()
    return pattern + re.escape(template[last:])


@lru_cache(maxsize=1024)
def template_regex(template: str) -> re.Pattern:
    """A regex for the paths that `template` matches."""
    try:
        return re.compile(template_pattern(template, keep_regex=True))
    except re.error:  # a Java regex Python cannot read: let that variable match any segment
        return re.compile(template_pattern(template, keep_regex=False))


def matches(template: str, path: str) -> bool:
    return template_regex(template).fullmatch(path) is not None


def segments(path: str) -> list[str]:
    """/users/{id} -> ["users", "{id}"]; a / at the end gives an empty last segment."""
    return (path[1:] if path.startswith("/") else path).split("/")


def has_wildcard(segment: str) -> bool:
    return any(char in segment for char in "{*?")


def filled(template: str, path: str) -> str:
    """The template with its wildcard segments taken from `path` where they line up: /users/5 for /users/{id}."""
    parts, given = segments(template), segments(path)
    if len(parts) != len(given):
        return template
    return "/" + "/".join(g if has_wildcard(p) and matches(p, g) else p for p, g in zip(parts, given))


def wanted(template: str, path: str) -> str:
    """`path` with the template's wildcard segments kept: /user/{id} for /user/1 against /users/{id}."""
    parts, given = segments(template), segments(path)
    if len(parts) != len(given):
        return path
    return "/" + "/".join(p if has_wildcard(p) else g for p, g in zip(parts, given))


def is_variable(segment: str, name: str) -> bool:
    """{id} or {id:\\d+} for `name` id."""
    return re.fullmatch(rf"\{{{re.escape(name)}(?::.*)?\}}", segment) is not None


def variable_value(template: str, path: str, name: str) -> str | None:
    """The value `path` gives the variable `name`: 5 for {id} in /users/{id} and /users/5."""
    parts, given = segments(template), segments(path)
    if len(parts) != len(given):
        return None
    return next((g for p, g in zip(parts, given) if is_variable(p, name)), None)


def with_variable(template: str, path: str, name: str, value: str) -> str | None:
    """`path` with the variable `name` set to `value`: /users/10 for /users/abc; None if it doesn't line up."""
    parts, given = segments(template), segments(path)
    if len(parts) != len(given) or not any(is_variable(p, name) for p in parts):
        return None
    return "/" + "/".join(value if is_variable(p, name) else g for p, g in zip(parts, given))


def edit_distance(a: str, b: str) -> int:
    """The fewest letters to add, remove or change to turn `a` into `b` (Levenshtein distance)."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def path_distance(path: str, template: str) -> int:
    return edit_distance(path, filled(template, path))


def ends_with_slash(path: str) -> bool:
    return len(path) > 1 and path.endswith("/")


def with_other_slash(path: str) -> str:
    """/users for /users/, and /users/ for /users."""
    return path[:-1] if ends_with_slash(path) else path + "/"


def differences(called: str, mapped: str) -> list[str]:
    """How `mapped` differs from `called`, letter by letter: ['"a" missing', 'extra "/" at the end']."""
    called_core = called[:-1] if ends_with_slash(called) else called
    mapped_core = mapped[:-1] if ends_with_slash(mapped) else mapped
    found = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, called_core, mapped_core, autojunk=False).get_opcodes():
        if tag == "delete":
            found.append(f'"{called_core[i1:i2]}" missing')
        elif tag == "insert":
            found.append(f'extra "{mapped_core[j1:j2]}"')
        elif tag == "replace":
            found.append(f'"{mapped_core[j1:j2]}" instead of "{called_core[i1:i2]}"')
    if ends_with_slash(mapped) and not ends_with_slash(called):
        found.append('extra "/" at the end')
    elif ends_with_slash(called) and not ends_with_slash(mapped):
        found.append('no "/" at the end')
    return found


def synonyms_of(word: str) -> list[str]:
    """The words that stand for `word` in a path, in the same case: Everything for All."""
    lower = word.lower()
    others = [other for group in SYNONYMS if lower in group for other in group if other != lower]
    return [other.capitalize() if word[0].isupper() else other for other in others]


def synonym_variants(path: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """`path` with words swapped for synonyms, each with its swaps: ("/users/1", [("user", "users")]) for /user/1."""
    words = [found for found in WORD.finditer(path) if synonyms_of(found[0])]
    variants = []
    for choice in product(*[[found[0], *synonyms_of(found[0])] for found in words]):
        swaps = [(found[0], new) for found, new in zip(words, choice) if new != found[0]]
        if not swaps:
            continue
        text, last = "", 0
        for found, new in zip(words, choice):
            text += path[last : found.start()] + new
            last = found.end()
        variants.append((text + path[last:], swaps))
    return variants
