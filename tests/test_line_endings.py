"""Nothing that runs on the Pi may carry CRLF into the index.

This is a regression test for a bug that cost two releases. `scripts/skylapse-admin`
was rewritten by a Windows editor and committed with `#!/usr/bin/env bash\r`.
Linux resolves a shebang literally, so the kernel looked for an interpreter
named `bash\r`, found none, and every privileged operation exited 127 --
including `restart-services`, which the updater treats as a failed build. 0.5.4
and 0.5.5 both rolled themselves back, correctly and unhelpfully: the rollback
restored the LF copy, so by the time anyone looked, the evidence was gone.

A `.gitattributes` rule already existed for this. It said `*.sh text eol=lf`,
and the one script with no extension was the one that broke. So this test keys
on what actually makes a file fragile -- a shebang, or being read by Linux
tooling -- rather than on what it happens to be called.

The index is what is checked, not the working tree: the index is what ships.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gz", ".xz", ".dng", ".woff", ".woff2"}


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=REPO, check=True,
                          stdout=subprocess.PIPE).stdout


def _tracked() -> list[str]:
    out = _git("ls-files").decode("utf-8", "replace")
    return [line for line in out.splitlines() if line.strip()]


def _staged_bytes(path: str) -> bytes:
    """The blob as it would be checked out on the Pi."""
    return subprocess.run(["git", "show", f":{path}"], cwd=REPO,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def _text_files() -> list[tuple[str, bytes]]:
    files = []
    for path in _tracked():
        if Path(path).suffix.lower() in BINARY_SUFFIXES:
            continue
        blob = _staged_bytes(path)
        if b"\0" in blob[:8192]:          # git's own binary heuristic
            continue
        files.append((path, blob))
    return files


def test_no_tracked_text_file_has_crlf():
    offenders = [p for p, blob in _text_files() if b"\r\n" in blob]
    assert not offenders, (
        "CRLF in the index; these are checked out verbatim on the Pi:\n  "
        + "\n  ".join(offenders)
        + "\nFix: `git add --renormalize .` (see .gitattributes)."
    )


def test_every_shebang_is_followed_by_a_bare_newline():
    """The specific failure, stated in its own terms.

    A file can be free of CRLF everywhere else and still be unrunnable if line
    one ends in \r, so this is worth asserting separately from the sweep above
    -- it is the assertion whose failure message names the actual symptom.
    """
    bad = []
    for path, blob in _text_files():
        if not blob.startswith(b"#!"):
            continue
        first = blob.split(b"\n", 1)[0]
        if first.endswith(b"\r"):
            bad.append(f"{path}: {first!r} -> Linux looks for an interpreter "
                       f"named {first.split(b'/')[-1]!r}")
    assert not bad, "CRLF shebang, 'bad interpreter' on the Pi:\n  " + "\n  ".join(bad)


def test_the_privileged_helper_is_executable_and_lf():
    """skylapse-admin is the single path to every root operation on the rig.

    It has no extension, which is exactly why the old extension-keyed rule
    missed it, so it gets named explicitly here as well as swept above.
    """
    entry = _git("ls-files", "-s", "scripts/skylapse-admin").decode().split()
    assert entry, "the privileged helper is not tracked"
    assert entry[0] == "100755", f"helper must be executable in git, got mode {entry[0]}"
    blob = _staged_bytes("scripts/skylapse-admin")
    assert blob.startswith(b"#!/usr/bin/env bash\n"), \
        f"helper shebang is {blob.splitlines()[0]!r}"


def test_gitattributes_defaults_all_text_to_lf():
    """The rule that keeps the two tests above passing without anyone thinking
    about it. Narrowing this back to a list of extensions is how 0.5.4 shipped."""
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attrs, \
        ".gitattributes must normalise all text to LF, not an extension list"
