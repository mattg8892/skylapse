"""Shared components must be called the way they are written.

Three blank screens now, all the same shape: the build passes, the tests pass,
the release goes out, and the settings page is black on the camera with the
reason only in the browser console.

  1. A regroup spliced out components that were still rendered. Fixed by
     test_frontend_components.py, which checks every `<Foo/>` is defined.
  2. A server error object was rendered as a React child. Fixed by
     test_error_rendering.py.
  3. `<Select>` was passed `<option>` children, but Select takes an `options`
     array and calls `options.map(...)`. `undefined.map` throws mid-render.

The first guard cannot catch the third: Select was imported and did exist. What
was wrong was the call, not the identity. So: for every shared component that
dereferences a prop without guarding it -- `options.map`, `x.length` -- every
call site has to pass that prop.

This is deliberately narrow. It only looks at components exported from
components/ui.jsx, and only at props whose absence is an immediate TypeError.
A broader "required props" check would need real type information and would
mostly produce noise.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "src"
UI = WEB / "components" / "ui.jsx"

EXPORTED = re.compile(r"export function ([A-Z]\w*)\s*\(\s*\{([^}]*)\}", re.S)


def _prop_names(destructured: str) -> list[str]:
    """Prop names from a destructuring pattern, ignoring defaults."""
    names = []
    for part in destructured.split(","):
        name = part.split("=")[0].split(":")[0].strip()
        if name and name.isidentifier():
            names.append(name)
    return names


def _body_after(src: str, start: int) -> str:
    """The function body following an `export function` match."""
    end = src.find("\nexport function", start + 1)
    return src[start:end if end != -1 else len(src)]


def required_props() -> dict[str, set[str]]:
    """Props each ui.jsx component dereferences without a guard.

    A default value in the signature makes it optional. `prop?.x` and
    `prop && prop.x` are guarded, so they do not count -- only a bare
    `prop.something` will throw when the prop is missing.
    """
    src = UI.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for match in EXPORTED.finditer(src):
        name, destructured = match.group(1), match.group(2)
        defaulted = {p.split("=")[0].strip()
                     for p in destructured.split(",") if "=" in p}
        body = _body_after(src, match.start())
        needed = set()
        for prop in _prop_names(destructured):
            if prop in defaulted or prop == "children":
                continue
            # A bare member access, not `prop?.` and not `prop &&`.
            if re.search(rf"\b{prop}\.\w", body) and not re.search(rf"\b{prop}\?\.", body):
                needed.add(prop)
        if needed:
            out[name] = needed
    return out


def jsx_usages(src: str, component: str) -> list[str]:
    """Every `<Component ...>` tag in full, brace-aware.

    A regex cannot do this: JSX attributes routinely contain `>` inside arrow
    functions, so `<Foo[^>]*>` stops at the first fat arrow and reports half a
    tag. Scan instead, tracking brace depth and quotes.
    """
    tags = []
    for m in re.finditer(rf"<{component}\b", src):
        i, depth, quote = m.end(), 0, ""
        while i < len(src):
            c = src[i]
            if quote:
                if c == quote and src[i - 1] != "\\":
                    quote = ""
            elif c in "\"'`":
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ">" and depth == 0:
                break
            i += 1
        tags.append(src[m.start():i + 1])
    return tags


REQUIRED = required_props()
SCREENS = sorted(WEB.rglob("*.jsx"))


def test_the_scanner_found_the_contract_it_is_policing():
    """If this ever comes back empty the whole file silently passes."""
    assert REQUIRED, "parsed no required props out of ui.jsx"
    assert "options" in REQUIRED.get("Select", set()), (
        "Select calls options.map(...), so `options` is required -- if this "
        "assertion fails the parser has stopped understanding ui.jsx")


@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.name)
def test_every_shared_component_gets_the_props_it_dereferences(path):
    src = path.read_text(encoding="utf-8")
    problems = []
    for component, props in sorted(REQUIRED.items()):
        for tag in jsx_usages(src, component):
            for prop in sorted(props):
                if not re.search(rf"\b{prop}\s*=", tag):
                    problems.append(
                        f"<{component}> without `{prop}`: {' '.join(tag.split())[:80]}")
    assert not problems, (
        f"{path.name} calls a shared component without a prop that component "
        f"dereferences. This builds cleanly and throws at render, which is a "
        f"blank screen:\n  " + "\n  ".join(problems))


# -- the scanner itself ------------------------------------------------------

def test_the_tag_scanner_survives_arrow_functions_in_attributes():
    """The reason this is not a regex. `[^>]*` stops at the arrow."""
    src = "<Select value={x} onChange={(v) => setX(v)} options={OPTS} />"
    tags = jsx_usages(src, "Select")
    assert len(tags) == 1
    assert "options={OPTS}" in tags[0], "tag was truncated at the fat arrow"


def test_the_scanner_catches_the_call_that_shipped():
    """Verbatim from 0.5.9. It builds, and it blanks the settings page."""
    broken = (
        "<Select value={testSeconds}\n"
        "  onChange={(e) => setTestSeconds(Number(e.target.value))}>\n"
        "  <option value={15}>15 seconds</option>\n"
        "</Select>"
    )
    tag = jsx_usages(broken, "Select")[0]
    assert not re.search(r"\boptions\s*=", tag), \
        "the guard must see that this call passes no options prop"


def test_the_scanner_accepts_the_repaired_call():
    fixed = ("<Select label=\"Test the heater for\" value={testSeconds}\n"
             "  onChange={(v) => setTestSeconds(Number(v))}\n"
             "  options={[{ value: 15, label: '15 seconds' }]} />")
    tag = jsx_usages(fixed, "Select")[0]
    assert re.search(r"\boptions\s*=", tag)
