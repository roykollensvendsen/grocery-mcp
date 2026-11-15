"""Every file is read and written as UTF-8, whatever the machine's language.

The bridge starts plugins with no language set, and Python then picks the
machine's default. A shop name with æ, ø or å would then break.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
TEXT_CALLS = {"read_text", "write_text", "open", "fdopen"}


def _calls_without_encoding(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in TEXT_CALLS:
            continue
        modes = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        mode = modes[0] if modes else ""
        if "b" in mode:
            continue
        if not any(k.arg == "encoding" for k in node.keywords):
            yield f"{path.relative_to(ROOT)}:{node.lineno} {name}()"


def test_every_file_is_read_and_written_as_utf_8():
    sources = sorted([*(ROOT / "src").rglob("*.py"), ROOT / "scripts/private_data_scan.py"])
    missing = [call for path in sources for call in _calls_without_encoding(path)]
    assert missing == [], "add encoding='utf-8' to: " + ", ".join(missing)
