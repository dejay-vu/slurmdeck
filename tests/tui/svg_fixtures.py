"""Deterministic, compact SVG normalization for Textual visual contracts."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

_TERMINAL_ID = re.compile(r"terminal-\d+")
_STYLE_RULE = re.compile(r"\.terminal-SNAPSHOT-(?:matrix|title|r\d+)\s*\{.*?\}", re.DOTALL)
_VOLATILE_AGE = re.compile(r"last success \S+ ago")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attributes(attributes: dict[str, str], *, omit: frozenset[str] = frozenset()) -> str:
    pairs = []
    for key, value in sorted(attributes.items()):
        if key in omit:
            continue
        normalized = _TERMINAL_ID.sub("terminal-SNAPSHOT", value)
        pairs.append(f'{key}="{html.escape(normalized, quote=True)}"')
    return " ".join(pairs)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _merged_rectangles(root: ET.Element) -> list[dict[str, str]]:
    rectangles = [
        dict(element.attrib)
        for element in root.iter()
        if _local_name(element.tag) == "rect"
        and ("fill" in element.attrib or "stroke" in element.attrib)
        and Decimal(element.attrib.get("width", "0")) > 0
    ]
    merged: list[dict[str, str]] = []
    for rectangle in rectangles:
        if merged:
            previous = merged[-1]
            same_style = {key: value for key, value in previous.items() if key not in {"x", "width"}} == {
                key: value for key, value in rectangle.items() if key not in {"x", "width"}
            }
            adjacent = Decimal(previous.get("x", "0")) + Decimal(previous["width"]) == Decimal(rectangle.get("x", "0"))
            if same_style and adjacent:
                previous["width"] = _decimal_text(Decimal(previous["width"]) + Decimal(rectangle["width"]))
                continue
        merged.append(rectangle)
    return merged


def normalize_svg_screenshot(svg: str) -> str:
    """Keep geometry, visible text, and used palette while removing random Rich IDs."""
    normalized_source = _TERMINAL_ID.sub("terminal-SNAPSHOT", svg)
    root = ET.fromstring(normalized_source)
    style = next(element for element in root.iter() if _local_name(element.tag) == "style")
    rules = [re.sub(r"\s+", " ", match.group(0)).strip() for match in _STYLE_RULE.finditer(style.text or "")]

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{html.escape(root.attrib["viewBox"], quote=True)}">',
        "  <style>",
        *(f"    {rule}" for rule in rules),
        "  </style>",
        '  <g class="terminal-SNAPSHOT-matrix">',
    ]
    for rectangle in _merged_rectangles(root):
        lines.append(f"    <rect {_attributes(rectangle)} />")
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "circle" and ("fill" in element.attrib or "stroke" in element.attrib):
            lines.append(f"    <{name} {_attributes(element.attrib)} />")
        elif name == "text":
            value = "".join(element.itertext()).replace("\N{NO-BREAK SPACE}", " ")
            if not value.strip():
                continue
            value = _VOLATILE_AGE.sub("last success AGE ago", value)
            attributes = _attributes(element.attrib, omit=frozenset({"clip-path"}))
            lines.append(f"    <text {attributes}>{html.escape(value)}</text>")
    lines.extend(["  </g>", "</svg>", ""])
    return "\n".join(lines)
