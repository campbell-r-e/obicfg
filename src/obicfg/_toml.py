"""TOML loading that works on any Python this tool supports.

``tomllib`` arrived in 3.11.  Plenty of the machines you would actually park
next to an ATA -- Alpine on an old netbook, a Debian oldstable box, a BSD
router -- ship something older, and the whole point of a stdlib-only tool is
that it runs there without a pip install.  So: use ``tomllib`` when it is
present, and fall back to a small parser covering the subset this tool's
config and profile files use (tables, key/value pairs, strings, integers,
booleans, and arrays of strings).

The fallback is deliberately strict.  Anything it does not understand raises
rather than guessing, so a file that parses here parses the same way under
``tomllib``.
"""

from __future__ import annotations

import re

try:  # pragma: no cover - exercised by whichever branch the runtime takes
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _tomllib = None  # type: ignore[assignment]


class TomlError(ValueError):
    """A config or profile file could not be parsed."""


_KEY = r"""(?:[A-Za-z0-9_-]+|"[^"]*"|'[^']*')"""
_TABLE_RE = re.compile(rf"^\[\s*({_KEY}(?:\s*\.\s*{_KEY})*)\s*\]$")
_PAIR_RE = re.compile(rf"^({_KEY}(?:\s*\.\s*{_KEY})*)\s*=\s*(.+?)\s*$")
_KEY_RE = re.compile(_KEY)

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "b": "\b",
    "f": "\f",
}


def loads(text: str) -> dict:
    """Parse TOML text into a dict."""
    if _tomllib is not None:
        try:
            return _tomllib.loads(text)
        except Exception as exc:  # tomllib raises TOMLDecodeError
            raise TomlError(str(exc)) from None
    return _fallback_loads(text)


def _split_key(raw: str) -> list[str]:
    parts = [p.strip() for p in _KEY_RE.findall(raw)]
    return [p[1:-1] if p[:1] in ("'", '"') else p for p in parts]


def _descend(root: dict, path: list[str]) -> dict:
    node = root
    for part in path:
        nxt = node.setdefault(part, {})
        if not isinstance(nxt, dict):
            raise TomlError(f"{part!r} is both a value and a table")
        node = nxt
    return node


def _parse_value(raw: str, line_no: int):
    raw = raw.strip()
    if not raw:
        raise TomlError(f"line {line_no}: missing value")
    if raw[0] in ('"', "'"):
        return _parse_string(raw, line_no)
    if raw[0] == "[":
        if not raw.endswith("]"):
            raise TomlError(
                f"line {line_no}: multi-line arrays are not supported by the "
                "built-in parser; install Python 3.11+ or the 'tomli' package"
            )
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(item, line_no) for item in _split_array(inner, line_no)]
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(raw, 10)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        raise TomlError(f"line {line_no}: cannot parse value {raw!r}") from None


def _parse_string(raw: str, line_no: int) -> str:
    quote = raw[0]
    if len(raw) < 2 or raw[-1] != quote:
        raise TomlError(f"line {line_no}: unterminated string {raw!r}")
    body = raw[1:-1]
    if quote == "'":
        return body  # literal string: no escape processing, per TOML
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise TomlError(f"line {line_no}: trailing backslash")
        code = body[index]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            index += 1
        elif code in ("u", "U"):
            width = 4 if code == "u" else 8
            hexits = body[index + 1 : index + 1 + width]
            if len(hexits) != width:
                raise TomlError(f"line {line_no}: truncated \\{code} escape")
            out.append(chr(int(hexits, 16)))
            index += 1 + width
        else:
            raise TomlError(f"line {line_no}: unknown escape \\{code}")
    return "".join(out)


def _split_array(inner: str, line_no: int) -> list[str]:
    items: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            buffer.append(char)
        elif char == ",":
            items.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    if quote:
        raise TomlError(f"line {line_no}: unterminated string in array")
    tail = "".join(buffer).strip()
    if tail:
        items.append(tail)
    return items


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _fallback_loads(text: str) -> dict:
    root: dict = {}
    table = root
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        table_match = _TABLE_RE.match(line)
        if table_match:
            table = _descend(root, _split_key(table_match.group(1)))
            continue
        pair_match = _PAIR_RE.match(line)
        if not pair_match:
            raise TomlError(f"line {line_no}: cannot parse {raw_line.strip()!r}")
        key_parts = _split_key(pair_match.group(1))
        target = _descend(table, key_parts[:-1])
        target[key_parts[-1]] = _parse_value(pair_match.group(2), line_no)
    return root


def flatten(node: dict, prefix: str = "") -> dict:
    """Flatten nested tables back into dotted keys.

    TOML treats ``sp2.Enable = true`` and ``"sp2.Enable" = true`` differently
    -- the first builds a nested table.  For a settings file addressed by
    dotted parameter paths that distinction is pure noise, so both spellings
    are collapsed to the same flat key here.
    """
    out: dict = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{path}."))
        else:
            out[path] = value
    return out
