"""Parsing of the OBi200's XML configuration pages.

Every admin page on the device is an XML document rendered client-side by
``default.xsl``.  The structure is regular enough to drive a whole CLI from:

    <model reboot_req="false">
      <object name="SIP" access="readWrite">
        <pagetitle>ITSP Profile B</pagetitle>
        <parameter name="ProxyServerPort" access="readWrite">
          <description>Destination port ...</description>
          <syntax><unsignedInt><range minInclusive="0" maxInclusive="65535"/></unsignedInt></syntax>
          <value hash="324f6706" type="input" default="5060" current="5080"/>
        </parameter>
      </object>
    </model>

Two details matter more than the rest:

* ``hash`` is the parameter's wire identity.  It is what a write is addressed
  to, and it is stable for a given firmware build but *not* documented
  anywhere, which is why the tool discovers them instead of hardcoding them.
* ``current`` is only present when the parameter has been changed away from
  its factory value.  Absent ``current`` means "still at default", so the
  effective value is ``default``.  Conflating the two is the single easiest
  way to write a broken diff.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .errors import ValidationError

#: Values the device stores but which the UI can never round-trip.
_SENTINEL_DEFAULT = "default"

_TRUE = {"true", "yes", "on", "1", "enable", "enabled"}
_FALSE = {"false", "no", "off", "0", "disable", "disabled"}


@dataclass(frozen=True)
class Syntax:
    """The declared type of a parameter, as the device describes it."""

    kind: str = "string"
    enumeration: tuple[str, ...] = ()
    min_inclusive: int | None = None
    max_inclusive: int | None = None
    max_length: int | None = None

    @property
    def is_bool(self) -> bool:
        return self.kind == "boolean"

    @property
    def is_int(self) -> bool:
        return self.kind in ("unsignedInt", "int")


@dataclass
class Parameter:
    """One configurable setting on one page."""

    page: str  # page filename without the .xml suffix, e.g. "VS_1_VP_2_SIP_"
    obj: str  # enclosing <object name=...>, e.g. "SIP"
    name: str  # e.g. "ProxyServerPort"
    hash: str  # e.g. "324f6706" -- the wire identity
    widget: str  # "input" | "list" | "bool" | ...
    default: str
    current: str | None  # None == parameter is still at its factory default
    access: str = "readWrite"
    description: str = ""
    syntax: Syntax = field(default_factory=Syntax)

    @property
    def path(self) -> str:
        """Canonical dotted address, e.g. ``VS_1_VP_2_SIP_.ProxyServerPort``."""
        return f"{self.page}.{self.name}"

    @property
    def value(self) -> str:
        """The value actually in force on the device right now."""
        return self.default if self.current is None else self.current

    @property
    def is_default(self) -> bool:
        return self.current is None

    @property
    def writable(self) -> bool:
        return self.access == "readWrite"

    def coerce(self, value: object) -> str:
        """Validate ``value`` against the device's own declared syntax.

        Returns the exact string the device expects.  Raising here is the
        whole point: a rejected write on an OBi looks identical to an accepted
        one (HTTP 200 either way), so anything catchable is caught before it
        reaches the wire.
        """
        if not self.writable:
            raise ValidationError(f"{self.path} is read-only")

        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)

        if text == _SENTINEL_DEFAULT:
            # The web UI overloads the literal string "default" as "restore
            # this parameter to its factory value" -- there is no escape for
            # it, so a genuine value of "default" is unrepresentable.
            raise ValidationError(
                f"{self.path}: the literal value 'default' is reserved by the "
                "device as the reset-to-factory sentinel; use `obicfg unset` "
                "if that is what you meant"
            )

        if self.syntax.is_bool or self.widget == "bool":
            low = text.strip().lower()
            if low in _TRUE:
                return "true"
            if low in _FALSE:
                return "false"
            raise ValidationError(f"{self.path}: expected a boolean, got {text!r}")

        if self.syntax.enumeration:
            for option in self.syntax.enumeration:
                if option.lower() == text.strip().lower():
                    return option  # canonical spelling, as the UI submits it
            raise ValidationError(
                f"{self.path}: {text!r} is not one of "
                + ", ".join(repr(o) for o in self.syntax.enumeration)
            )

        if self.syntax.is_int:
            try:
                number = int(text.strip())
            except ValueError:
                raise ValidationError(
                    f"{self.path}: expected an integer, got {text!r}"
                ) from None
            lo, hi = self.syntax.min_inclusive, self.syntax.max_inclusive
            if lo is not None and number < lo:
                raise ValidationError(f"{self.path}: {number} is below minimum {lo}")
            if hi is not None and number > hi:
                raise ValidationError(f"{self.path}: {number} is above maximum {hi}")
            return str(number)

        if self.syntax.max_length is not None and len(text) > self.syntax.max_length:
            # The declared maximum is not always enforced by the device: the
            # OBiPLUS inbound route ships holding 276 characters on a page
            # that declares 128. Where the current value already exceeds the
            # limit, the limit is demonstrably advisory, and refusing would
            # make that parameter uneditable by this tool -- including putting
            # back what is already there.
            if len(self.value) > self.syntax.max_length:
                return text
            raise ValidationError(
                f"{self.path}: value is {len(text)} characters, "
                f"maximum is {self.syntax.max_length}"
            )
        return text


@dataclass
class Page:
    """A parsed configuration page."""

    name: str  # filename without .xml
    title: str
    description: str
    reboot_required: bool
    parameters: list[Parameter]

    @property
    def filename(self) -> str:
        return f"{self.name}.xml"

    def get(self, name: str) -> Parameter | None:
        """First parameter matching ``name``, case-insensitively.

        Names are **not** unique per page -- a codec profile repeats
        ``BitRate`` once per codec -- so this is only safe where the caller
        knows the name is unique.  Anything acting on user input should use
        :meth:`candidates` and refuse to guess.
        """
        wanted = name.lower()
        for param in self.parameters:
            if param.name.lower() == wanted:
                return param
        for param in self.parameters:
            if f"{param.obj}.{param.name}".lower() == wanted:
                return param
        return None

    def candidates(self, name: str) -> list:
        """Every parameter a name could mean, in document order.

        Names are not unique per page.  A codec profile carries five
        ``BitRate`` parameters -- one per codec -- and only one of them is
        writable, so answering with the first match silently points a write at
        the wrong codec.  Callers that must not guess use this instead of
        :meth:`get`.
        """
        wanted = name.lower()
        exact = [
            param
            for param in self.parameters
            if f"{param.obj}.{param.name}".lower() == wanted
        ]
        if exact:
            return exact
        return [param for param in self.parameters if param.name.lower() == wanted]

    def by_hash(self, hash_: str) -> Parameter | None:
        return next((p for p in self.parameters if p.hash == hash_), None)


def _text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _parse_syntax(node: ET.Element | None) -> Syntax:
    if node is None:
        return Syntax()
    for child in node:
        kind = child.tag
        enumeration = tuple(
            e.get("value", "") for e in child.findall("enumeration")
        )
        size = child.find("size")
        rng = child.find("range")

        def _int(el: ET.Element | None, attr: str) -> int | None:
            if el is None or el.get(attr) is None:
                return None
            try:
                return int(el.get(attr, ""))
            except ValueError:
                return None

        return Syntax(
            kind=kind,
            enumeration=enumeration,
            min_inclusive=_int(rng, "minInclusive"),
            max_inclusive=_int(rng, "maxInclusive"),
            max_length=_int(size, "maxLength"),
        )
    return Syntax()


def _walk(node: ET.Element, obj: str, out: list[tuple[str, ET.Element]]) -> None:
    for child in node:
        if child.tag == "object":
            _walk(child, child.get("name", obj), out)
        elif child.tag == "parameter":
            out.append((obj, child))
        else:
            _walk(child, obj, out)


def parse_page(name: str, xml: bytes | str) -> Page:
    """Parse one configuration page.

    ``name`` is the page filename, with or without the ``.xml`` suffix.
    """
    if name.endswith(".xml"):
        name = name[: -len(".xml")]
    root = ET.fromstring(xml)

    pairs: list[tuple[str, ET.Element]] = []
    _walk(root, root.get("name", ""), pairs)

    parameters: list[Parameter] = []
    for obj, node in pairs:
        value = node.find("value")
        if value is None or not value.get("hash"):
            continue  # display-only rows; nothing addressable to write
        parameters.append(
            Parameter(
                page=name,
                obj=obj,
                name=node.get("name", ""),
                hash=value.get("hash", ""),
                widget=value.get("type", "input"),
                default=value.get("default", ""),
                current=value.get("current"),
                access=node.get("access", "readWrite"),
                description=_text(node.find("description")),
                syntax=_parse_syntax(node.find("syntax")),
            )
        )

    title = ""
    description = ""
    for element in root.iter():
        if element.tag == "pagetitle" and not title:
            title = _text(element)
        elif element.tag == "description" and not description:
            description = _text(element)
        if title and description:
            break

    return Page(
        name=name,
        title=title or name,
        description=description,
        reboot_required=root.get("reboot_req", "false").lower() == "true",
        parameters=parameters,
    )


def parse_objects(xml: bytes | str) -> list[tuple[str, dict[str, str]]]:
    """``(object name, {parameter: effective value})`` in document order.

    :func:`parse_page` flattens a page into one list of parameters, which is
    what you want for configuration.  The status pages need the opposite: the
    call-quality page carries one ``<object name="RTP Statistics">`` block
    *per service*, all with identical parameter names, so the block boundaries
    carry the meaning.  Grouping by name would collapse SP1's counters onto
    SP4's; this keeps repeats distinct and ordered.
    """
    root = ET.fromstring(xml)
    out: list[tuple[str, dict[str, str]]] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            if child.tag == "object":
                entries: dict[str, str] = {}
                for parameter in child.findall("parameter"):
                    value = parameter.find("value")
                    if value is None:
                        continue
                    current = value.get("current")
                    entries[parameter.get("name", "")] = (
                        value.get("default", "") if current is None else current
                    )
                out.append((child.get("name", ""), entries))
                walk(child)
            else:
                walk(child)

    walk(root)
    return out


_MENU_ENTRY = re.compile(
    r"""onclick\s*=\s*["']e\(\s*['"]([A-Za-z0-9_]+\.xml)['"]\s*\)["'][^>]*>([^<]*)<""",
    re.IGNORECASE,
)


def parse_menu(html: bytes | str) -> list[tuple[str, str]]:
    """Extract ``(page filename, menu label)`` pairs from ``menu.htm``.

    The menu is the device's own statement of which pages exist on this
    model and firmware, which beats any list this tool could hardcode.
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    seen: dict[str, str] = {}
    for filename, label in _MENU_ENTRY.findall(html):
        seen.setdefault(filename, label.strip())
    return list(seen.items())
