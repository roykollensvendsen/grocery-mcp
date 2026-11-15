"""The layers: the core, then the server and the command line on top.

The core reads Kassalapp, keeps the cache and the call counts, and words the
answers. It knows no MCP, so it can be tested and reused without a server.
Only the top serves it over MCP and the command line. Every import, including
those made only for type checking or inside a function, is counted.

The map of the layers is in `scripts/ratchet.py`, which also counts the breaches
for the code-health page; here any breach at all is refused.
"""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
PACKAGE = ROOT / "src" / "grocery_mcp"


def _ratchet():
    spec = importlib.util.spec_from_file_location("ratchet", ROOT / "scripts/ratchet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ratchet = _ratchet()


def test_every_module_has_a_layer_and_imports_only_what_its_layer_may():
    assert ratchet.layer_breaches(PACKAGE) == []


def test_the_core_never_imports_mcp():
    core = [p for p in PACKAGE.rglob("*.py") if ratchet.layer(p.relative_to(PACKAGE).stem) == "core"]
    assert core, "no core modules found; the layer map is out of date"
    assert "mcp" in ratchet.FORBIDDEN_OUTSIDE["core"]


def test_the_answers_never_import_the_web_library():
    """What is said is decided apart from how prices are fetched, so another source can share it.

    Only a price source talks to the network. The answers catch what a source raises,
    never the web library's own errors, ready for sources that are not Kassalapp.
    """
    import ast  # noqa: PLC0415

    tree = ast.parse((PACKAGE / "answers.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {
        (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.level == 0
    }
    assert "httpx" not in imported
