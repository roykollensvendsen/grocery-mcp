#!/usr/bin/env python3
"""Refuse a commit that would publish where the owner shops or lives, or their key.

This repository will be public. Nothing in it may name the owner's shops, their
street or their town, nor hold their Kassalapp key. The checks, on every file
given (by default the files staged for commit), or on the whole history:

- no home-network address: a private IPv4 address (examples use 192.0.2.x);
- no street address, such as a Norwegian street name followed by a number;
- no coordinates more exact than two decimals;
- no word from the owner's private list, such as a shop's own name;
- not the owner's Kassalapp key.

The private list is ``~/.config/kassalapp-mcp/private.toml``, holding
``words = ["...", ...]``. It lives outside the repository, so the list itself is
never published. The key is checked only when ``--key-file`` names it, so the
commit hook never reads it; the owner runs that check once before a push.

What is found is reported by kind and line, never quoted, since the report may
land in a public log.

    python3 scripts/private_data_scan.py            # the staged files (pre-commit)
    python3 scripts/private_data_scan.py --tracked  # every tracked file
    python3 scripts/private_data_scan.py --history --key-file ~/.config/kassalapp-mcp/key
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORDS = pathlib.Path("~/.config/kassalapp-mcp/private.toml")
#: Shorter words would match ordinary text; shorter keys are not keys.
MIN_WORD = 4
MIN_KEY = 6

PRIVATE_IPV4 = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])"
)
STREET = re.compile(
    r"\b[A-ZÆØÅ][a-zæøå]+(?:gata|gaten|gate|veien|vegen|vei|veg|torget|plassen|allé|alleen)"
    r"\s+\d{1,4}[A-Za-z]?\b"
)
#: Norway lies within about 57-72 north and 4-32 east.
COORDINATES = re.compile(r"\b(?:5[7-9]|6\d|7[0-2])\.\d{3,}\s*,\s*(?:[4-9]|[12]\d|3[0-2])\.\d{3,}\b")


class BrokenListError(ValueError):
    """The private list exists but can't be used, so its words would go unchecked."""


def private_values(words: pathlib.Path, key: pathlib.Path | None) -> set[str]:
    """The owner's private words, and their key, from files outside the repository."""
    out: set[str] = set()
    if words.is_file():
        try:
            listed = tomllib.loads(words.read_text(encoding="utf-8")).get("words", [])
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            # The parser's own message is left out: it may quote a word from the list.
            message = f"{words.name} can't be read as TOML"
            raise BrokenListError(message) from error
        if not isinstance(listed, list):
            message = f"{words.name} must hold words = [...], a list of words"
            raise BrokenListError(message)
        out |= {w for w in listed if isinstance(w, str) and len(w) >= MIN_WORD}
    secret = key.read_text(encoding="utf-8").strip() if key is not None and key.is_file() else ""
    if len(secret) >= MIN_KEY:
        out.add(secret)
    return out


def offences(text: str, private: set[str]) -> list[str]:
    """What in ``text`` would say where the owner shops or lives, by kind only."""
    found = [f"line {_line(text, m.start())}: a home-network address" for m in PRIVATE_IPV4.finditer(text)]
    found += [f"line {_line(text, m.start())}: a street address" for m in STREET.finditer(text)]
    found += [f"line {_line(text, m.start())}: exact coordinates" for m in COORDINATES.finditer(text)]
    lowered = text.lower()
    for value in private:
        at = lowered.find(value.lower())
        if at >= 0:
            found.append(f"line {_line(text, at)}: a private word or the key")
    return found


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def _git(*args: str) -> str:
    command = ["git", *args]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", check=True)  # noqa: S603
    return done.stdout


def _texts(*, tracked: bool, history: bool) -> list[tuple[str, str]]:
    """Each place to scan, as (what it is, its text)."""
    if history:
        return [("the history", _git("log", "-p", "--all", "--no-color", "--format=commit %H%n%B"))]
    names = _git("ls-files") if tracked else _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    out = []
    for name in names.splitlines():
        path = ROOT / name
        try:
            out.append((name, path.read_text(encoding="utf-8")))
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    """Scan the staged files, every tracked file, or the history; 1 when anything is found."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tracked", action="store_true", help="scan every tracked file")
    parser.add_argument("--history", action="store_true", help="scan every commit on every branch")
    parser.add_argument("--words", default=str(WORDS), help=f"the private list (default {WORDS})")
    parser.add_argument("--key-file", help="also refuse this Kassalapp key (by default it is not read)")
    args = parser.parse_args(argv)
    key = pathlib.Path(args.key_file).expanduser() if args.key_file else None
    try:
        private = private_values(pathlib.Path(args.words).expanduser(), key)
    except BrokenListError as error:
        # Refused rather than skipped: a commit checked without the list could publish a word from it.
        sys.stderr.write(f"{error}; fix it, then commit again. Nothing was checked.\n")
        return 1
    bad = 0
    for where, text in _texts(tracked=args.tracked, history=args.history):
        for offence in offences(text, private):
            sys.stderr.write(f"{where}, {offence}\n")
            bad += 1
    if bad:
        sys.stderr.write("Use made-up names, 192.0.2.x, and two decimals at most in examples.\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
