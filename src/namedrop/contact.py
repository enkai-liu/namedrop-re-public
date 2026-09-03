"""Build and parse the vCard payload that NameDrop exchanges.

Apple Contacts emits vCard 3.0. We keep a minimal, dependency-free implementation that
round-trips the fields NameDrop actually carries (name, phones, emails, org). If we later
need full fidelity (photos, postal addresses, custom labels) we can swap in `vobject`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _escape(value: str) -> str:
    # vCard text escaping per RFC 6350 / 2426.
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _split_components(value: str, sep: str = ";") -> list[str]:
    """Split a structured value on *unescaped* `sep`, leaving escape sequences intact."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            buf.append(value[i : i + 2])  # keep the escape pair for _unescape later
            i += 2
            continue
        if c == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


@dataclass
class Contact:
    """A contact, the way NameDrop cares about it."""

    first_name: str = ""
    last_name: str = ""
    organization: str = ""
    phones: list[str] = field(default_factory=list)   # ("CELL", "+1...") if you want labels later
    emails: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    def to_vcard(self) -> str:
        """Serialize to a vCard 3.0 string (CRLF line endings, as Apple emits)."""
        lines = ["BEGIN:VCARD", "VERSION:3.0"]
        # N = structured name: Family;Given;Additional;Prefix;Suffix
        lines.append(f"N:{_escape(self.last_name)};{_escape(self.first_name)};;;")
        lines.append(f"FN:{_escape(self.full_name)}")
        if self.organization:
            lines.append(f"ORG:{_escape(self.organization)}")
        for phone in self.phones:
            lines.append(f"TEL;TYPE=CELL:{_escape(phone)}")
        for email in self.emails:
            lines.append(f"EMAIL;TYPE=INTERNET:{_escape(email)}")
        lines.append("END:VCARD")
        return "\r\n".join(lines) + "\r\n"

    def to_bytes(self) -> bytes:
        return self.to_vcard().encode("utf-8")

    @classmethod
    def from_vcard(cls, text: str | bytes) -> "Contact":
        """Parse the subset of vCard we care about. Tolerant of unknown lines."""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        c = cls()
        for raw in text.replace("\r\n", "\n").split("\n"):
            if not raw or ":" not in raw:
                continue
            prop, value = raw.split(":", 1)  # value still escaped; split structure first
            name = prop.split(";", 1)[0].upper()
            if name == "N":
                parts = _split_components(value, ";")
                c.last_name = _unescape(parts[0]) if len(parts) > 0 else ""
                c.first_name = _unescape(parts[1]) if len(parts) > 1 else ""
            elif name == "FN" and not (c.first_name or c.last_name):
                # fall back to FN only if N didn't give us anything
                bits = _unescape(value).split(" ", 1)
                c.first_name = bits[0]
                c.last_name = bits[1] if len(bits) > 1 else ""
            elif name == "ORG":
                c.organization = _unescape(_split_components(value, ";")[0])
            elif name == "TEL":
                c.phones.append(_unescape(value))
            elif name == "EMAIL":
                c.emails.append(_unescape(value))
        return c
