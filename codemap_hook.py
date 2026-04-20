#!/usr/bin/env python3
"""Claude-Code-Mapper: hook-driven, cross-session code-map cache for Claude Code.

Subcommands:
    pre     Run as PreToolUse hook for the Read tool.
    post    Run as PostToolUse hook for the Read tool.
    status  Print cache summary.
    clear   Delete the cache directory.
    show    Print the cached code map for one path.

Cache layout (under CODEMAP_CACHE_DIR, default ~/.claude/codemap-cache):
    index.json         -> { abs_path: { hash, size, mtime, map_file, generated_at } }
    maps/<key>.json    -> code map for that file
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CACHE_DIR = Path(os.environ.get("CODEMAP_CACHE_DIR") or Path.home() / ".claude" / "codemap-cache")
MAPS_DIR = CACHE_DIR / "maps"
INDEX_FILE = CACHE_DIR / "index.json"

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
FALLBACK_PREVIEW_LINES = 80
HASH_CHUNK = 65536

# Bump when the on-disk map schema changes so old cache entries are invalidated cleanly.
# v1 = original verbose format, v2 = grouped-by-kind with short field aliases.
CODEMAP_VERSION = 2

# Short aliases used in compact maps.
_KEY_MAP = {"name": "n", "line": "l", "sig": "s", "doc": "d",
            "end_line": "e", "scope": "c", "type": "y"}

CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".cs", ".java", ".kt", ".go", ".rs",
    ".rb", ".php", ".swift", ".scala", ".lua",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".ex", ".exs", ".erl", ".hs", ".ml", ".fs", ".fsx", ".fsi",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1",
    ".sql", ".vue", ".svelte",
    ".gd", ".gdscript", ".nim", ".dart", ".zig",
    # .NET / MAUI ecosystem
    ".xaml", ".axaml", ".razor", ".cshtml", ".vb",
}

XAML_EXTENSIONS = {".xaml", ".axaml"}


# ---------- utilities ----------

def ensure_dirs() -> None:
    MAPS_DIR.mkdir(parents=True, exist_ok=True)


def load_index() -> dict[str, Any]:
    if not INDEX_FILE.exists():
        return {}
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_index(index: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_path(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


def cache_key(abs_path: str) -> str:
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]


def is_cache_valid(abs_path: str, entry: dict[str, Any]) -> bool:
    """Fast-path via (size, mtime); fall back to hash for correctness."""
    try:
        stat = os.stat(abs_path)
    except OSError:
        return False
    if stat.st_size == entry.get("size") and stat.st_mtime == entry.get("mtime"):
        return True
    current_hash = sha256_of_file(abs_path)
    if current_hash == entry.get("hash"):
        # Touched (e.g. git checkout) but content identical: refresh quick-check fields.
        entry["size"] = stat.st_size
        entry["mtime"] = stat.st_mtime
        return True
    return False


# ---------- parsers ----------

def has_ctags() -> bool:
    try:
        subprocess.run(
            ["ctags", "--version"],
            capture_output=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def run_ctags(path: str) -> list[dict[str, Any]] | None:
    try:
        result = subprocess.run(
            [
                "ctags",
                "--output-format=json",
                "--fields=+neKS",
                "--extras=+q",
                "-f", "-",
                path,
            ],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    entries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def build_codemap_ctags(abs_path: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    language = None
    for e in entries:
        if not language:
            language = e.get("language")
        sym: dict[str, Any] = {
            "kind": e.get("kind", "unknown"),
            "name": e.get("name", ""),
            "line": e.get("line", 0),
        }
        if "scope" in e:
            sym["scope"] = e["scope"]
        if "signature" in e:
            sym["sig"] = e["signature"]
        if "typeref" in e:
            sym["type"] = e["typeref"].replace("typename:", "")
        if "end" in e:
            sym["end_line"] = e["end"]
        symbols.append(sym)
    symbols.sort(key=lambda s: s.get("line", 0))
    return {
        "path": abs_path,
        "lang": language or "unknown",
        "source": "ctags",
        "symbols": symbols,
    }


# ---------- fallback parser ----------

# Lightweight regex-based symbol sniffer for when ctags is unavailable.
# Not perfect, but catches the common cases for TS/JS/Python/C#/Go/Java/etc.
FALLBACK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)")),
    ("arrow_fn", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?\(?[^)]*\)?\s*=>")),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:public\s+|internal\s+|private\s+|protected\s+|abstract\s+|static\s+|sealed\s+|partial\s+)*class\s+(\w+)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?(?:public\s+)?interface\s+(\w+)")),
    ("struct", re.compile(r"^\s*(?:pub\s+)?(?:public\s+)?struct\s+(\w+)")),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:public\s+)?enum\s+(\w+)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*=")),
    ("py_def", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)")),
    ("py_class", re.compile(r"^\s*class\s+(\w+)")),
    ("cs_method", re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|async|virtual|override|sealed|abstract|readonly|extern|unsafe|partial|new)\s+)+"
        r"[\w<>\[\],.?]+(?:\s*\[\])?\s+(\w+)\s*\(([^)]*)\)\s*(?:where[^{;]*)?[{;]?\s*$"
    )),
    ("cs_ctor", re.compile(
        r"^\s*(?:public|private|protected|internal)\s+([A-Z]\w*)\s*\(([^)]*)\)\s*(?::\s*(?:base|this)\s*\([^)]*\))?\s*\{?\s*$"
    )),
    # C# record (with optional primary ctor). Matches: record Foo, record class Foo, record struct Foo(...).
    ("cs_record", re.compile(
        r"^\s*(?:(?:public|private|protected|internal|sealed|abstract|partial)\s+)*"
        r"record(?:\s+(?:class|struct))?\s+(\w+)(?:\s*\(([^)]*)\))?"
    )),
    # C# property with auto-accessor body: public int Foo { get; set; }
    ("cs_property", re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|readonly|required|new|sealed)\s+)+"
        r"[\w<>\[\],.?]+\??\s+(\w+)\s*\{\s*(?:get|set|init)"
    )),
    # C# event
    ("cs_event", re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract)\s+)+"
        r"event\s+[\w<>.?]+\s+(\w+)"
    )),
    # C# namespace (block or file-scoped)
    ("cs_namespace", re.compile(r"^\s*namespace\s+([\w.]+)\s*[;{]?\s*$")),
    # C# delegate
    ("cs_delegate", re.compile(
        r"^\s*(?:(?:public|private|protected|internal)\s+)*"
        r"delegate\s+[\w<>\[\].?]+\s+(\w+)\s*\(([^)]*)\)"
    )),
    # TS decorator (on its own line above a class/method)
    ("ts_decorator", re.compile(r"^\s*@([A-Z]\w*)\s*\(")),
    # TS/JS class method (indented, inside a class). Conservative: requires return-type or body brace.
    ("ts_method", re.compile(
        r"^\s{2,}(?:(?:public|private|protected|static|async|readonly|override|abstract)\s+)*"
        r"(?:async\s+)?(\w+)\s*(?:<[^>]{1,60}>)?\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\]|&\s,.?]+)?\s*[\{;]"
    )),
    ("go_func", re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)")),
    ("gd_func", re.compile(r"^\s*(?:static\s+)?func\s+(\w+)\s*\(([^)]*)\)")),
    ("gd_class", re.compile(r"^\s*class_name\s+(\w+)")),
    ("rust_fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)")),
]


def build_codemap_fallback(abs_path: str, lines: list[str]) -> dict[str, Any]:
    ext = Path(abs_path).suffix.lower()
    # Patterns prefixed with a lang tag (cs_, ts_, gd_, ...) are tried ONLY on files of
    # that language; otherwise a ts_method regex would false-positive on a C# `if (...)` block.
    # Generic patterns (function, class, interface, ...) are always tried.
    lang_prefix = {
        ".gd": ("gd_",), ".go": ("go_",), ".rs": ("rust_",),
        ".py": ("py_",),
        ".cs": ("cs_",), ".razor": ("cs_",), ".cshtml": ("cs_",),
        ".ts": ("ts_",), ".tsx": ("ts_",), ".js": ("ts_",), ".jsx": ("ts_",),
        ".mjs": ("ts_",), ".cjs": ("ts_",),
    }
    all_prefixes = {"gd_", "go_", "rust_", "py_", "cs_", "ts_"}
    allowed = set(lang_prefix.get(ext, ()))
    patterns = [
        (k, p) for (k, p) in FALLBACK_PATTERNS
        if not any(k.startswith(px) for px in all_prefixes) or any(k.startswith(px) for px in allowed)
    ]
    # Prefer language-specific patterns first so they beat generic 'class'/'function' on a tie.
    patterns.sort(key=lambda kp: 0 if any(kp[0].startswith(px) for px in allowed) else 1)

    symbols: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n")
        for kind, pat in patterns:
            m = pat.match(line)
            if not m:
                continue
            name = m.group(1)
            if (name, i) in seen:
                continue
            seen.add((name, i))
            sym: dict[str, Any] = {"kind": kind, "name": name, "line": i}
            if m.lastindex and m.lastindex >= 2:
                params = m.group(2).strip()
                if params:
                    sym["sig"] = f"({params})"
            symbols.append(sym)
            break

    codemap: dict[str, Any] = {
        "path": abs_path,
        "lang": ext.lstrip(".") or "plain",
        "source": "fallback-regex",
        "lines": len(lines),
        "symbols": symbols,
    }
    if not symbols:
        # Nothing parseable: keep a small preview so Claude still has a signal.
        codemap["preview"] = "".join(lines[:FALLBACK_PREVIEW_LINES])
        codemap["note"] = (
            f"No parser matched; showing first {FALLBACK_PREVIEW_LINES} lines as preview. "
            "Install universal-ctags for a real code map."
        )
    return codemap


# ---------- enrichment (docstrings, imports, TODOs, header) ----------

TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|NOTE)\b\s*[:\-]?\s*(.+?)\s*$", re.IGNORECASE)

IMPORT_RE_TSJS = re.compile(r'^\s*import\s+(?:.+?\s+from\s+)?["\']([^"\']+)["\']')
# Angular-style multi-line imports:  import {\n  Foo,\n  Bar,\n} from '@pkg/x';
# The closing line starts with `}` and ends with `from "..."`.
IMPORT_RE_TSJS_MULTILINE_CLOSE = re.compile(r'^\s*\}\s*from\s+["\']([^"\']+)["\']')
IMPORT_RE_PY = re.compile(r"^\s*(?:from\s+(\S+)\s+import\b|import\s+(\S+))")
IMPORT_RE_CS = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?([\w.]+)\s*[;=]")
IMPORT_RE_JAVA = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;")
IMPORT_RE_RS = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:{}*,\s]+);")
IMPORT_RE_GO = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"')

DOC_LINE_SLASH = re.compile(r"^\s*(///?|//)\s?(.*)$")
DOC_LINE_HASH = re.compile(r"^\s*#\s?(.*)$")
DOC_BLOCK_STAR = re.compile(r"^\s*\*\s?(.*?)\*?/?\s*$")


def _truncate(text: str, limit: int = 280) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def extract_imports(lines: list[str], ext: str) -> list[str]:
    """Return up to 30 import targets from the top of the file."""
    out: list[str] = []
    seen: set[str] = set()
    scan = lines[:300]  # imports cluster near the top

    if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"}:
        # TS/JS needs two patterns: single-line `import X from 'y'` and the closing
        # `} from 'y'` of a multi-line import block (Angular/React style).
        for line in scan:
            for pat in (IMPORT_RE_TSJS, IMPORT_RE_TSJS_MULTILINE_CLOSE):
                m = pat.match(line)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    out.append(m.group(1).strip())
                    break
            if len(out) >= 30:
                break
        return out
    if ext == ".py":
        pat = IMPORT_RE_PY
        groups = (1, 2)
    elif ext in {".cs", ".razor", ".cshtml", ".vb"}:
        pat = IMPORT_RE_CS
        groups = (1,)
    elif ext in {".java", ".kt", ".scala"}:
        pat = IMPORT_RE_JAVA
        groups = (1,)
    elif ext == ".rs":
        pat = IMPORT_RE_RS
        groups = (1,)
    elif ext == ".go":
        pat = IMPORT_RE_GO
        groups = (1,)
    else:
        return out

    for line in scan:
        m = pat.match(line)
        if not m:
            continue
        for g in groups:
            val = m.group(g)
            if val and val not in seen:
                seen.add(val)
                out.append(val.strip())
                break
        if len(out) >= 30:
            break
    return out


def extract_todos(lines: list[str]) -> list[dict[str, Any]]:
    """Pick up TODO/FIXME/HACK/XXX/NOTE markers with surrounding context."""
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines, start=1):
        m = TODO_RE.search(line)
        if not m:
            continue
        text = m.group(2).strip()
        text = re.sub(r"(?:\*+/|-+|\*+)\s*$", "", text).strip()
        if text:
            out.append({"line": i, "kind": m.group(1).upper(), "text": _truncate(text, 150)})
        if len(out) >= 25:
            break
    return out


def extract_header_doc(lines: list[str], ext: str) -> str | None:
    """Leading comment or docstring at the top of the file."""
    for i, raw in enumerate(lines[:40]):
        line = raw.strip()
        if not line:
            continue
        # Block comment /** ... */ or /* ... */
        if line.startswith("/*"):
            collected: list[str] = []
            for l in lines[i : i + 40]:
                cleaned = re.sub(r"^\s*/?\*+/?\s?", "", l).rstrip()
                cleaned = cleaned.rstrip("*/").rstrip()
                if cleaned:
                    collected.append(cleaned)
                if "*/" in l:
                    break
            return _truncate(" ".join(collected))
        if line.startswith("///") or line.startswith("//"):
            collected = []
            for l in lines[i : i + 40]:
                m = DOC_LINE_SLASH.match(l)
                if not m:
                    break
                collected.append(m.group(2).strip())
            return _truncate(" ".join(c for c in collected if c))
        if ext == ".py" and (line.startswith('"""') or line.startswith("'''")):
            quote = line[:3]
            if line.endswith(quote) and len(line) > 3:
                return _truncate(line.strip(quote))
            collected = [line.lstrip(quote)]
            for l in lines[i + 1 : i + 40]:
                if quote in l:
                    collected.append(l.split(quote)[0])
                    break
                collected.append(l)
            return _truncate(" ".join(c.strip() for c in collected))
        if line.startswith("#") and ext in {".py", ".sh", ".bash", ".zsh", ".rb"}:
            # Skip shebang
            if line.startswith("#!"):
                continue
            collected = []
            for l in lines[i : i + 20]:
                m = DOC_LINE_HASH.match(l)
                if not m:
                    break
                collected.append(m.group(1).strip())
            return _truncate(" ".join(c for c in collected if c))
        # First non-blank, non-comment line — no header.
        return None
    return None


def extract_doc_above(lines: list[str], symbol_line: int) -> str | None:
    """Grab a doc comment block immediately above symbol_line (1-indexed)."""
    if symbol_line <= 1 or symbol_line > len(lines):
        return None
    idx = symbol_line - 2  # 0-indexed line directly above
    # Tolerate exactly one blank line between comment and symbol
    if idx >= 0 and lines[idx].strip() == "":
        idx -= 1
    if idx < 0:
        return None

    line = lines[idx].rstrip()
    stripped = line.strip()

    # Block comment: */ on the line above; walk back to /* or /**
    if stripped.endswith("*/"):
        collected: list[str] = []
        while idx >= 0:
            l = lines[idx].rstrip()
            cleaned = l
            if cleaned.rstrip().endswith("*/"):
                cleaned = cleaned.rstrip()[:-2]
            cleaned = re.sub(r"^\s*/?\*+\s?", "", cleaned).strip()
            if cleaned:
                collected.append(cleaned)
            if "/*" in l:
                break
            idx -= 1
        if collected:
            return _truncate(" ".join(reversed(collected)))
        return None

    # Line-comment blocks: //, ///, or #
    if stripped.startswith("///") or stripped.startswith("//"):
        collected = []
        while idx >= 0:
            m = DOC_LINE_SLASH.match(lines[idx])
            if not m:
                break
            collected.append(m.group(2).strip())
            idx -= 1
        if collected:
            return _truncate(" ".join(reversed([c for c in collected if c])))
        return None

    if stripped.startswith("#") and not stripped.startswith("#!"):
        collected = []
        while idx >= 0:
            m = DOC_LINE_HASH.match(lines[idx])
            if not m:
                break
            collected.append(m.group(1).strip())
            idx -= 1
        if collected:
            return _truncate(" ".join(reversed([c for c in collected if c])))
    return None


def extract_py_docstring(lines: list[str], symbol_line: int) -> str | None:
    """Python: look for a triple-quoted docstring on the first non-blank line inside the def/class body."""
    if symbol_line <= 0 or symbol_line >= len(lines):
        return None
    for i in range(symbol_line, min(symbol_line + 4, len(lines))):
        l = lines[i].strip()
        if not l:
            continue
        if l.startswith('"""') or l.startswith("'''"):
            quote = l[:3]
            body = l[3:]
            if body.endswith(quote) and body != "":
                return _truncate(body[:-3])
            collected = [body]
            for j in range(i + 1, min(i + 30, len(lines))):
                l2 = lines[j]
                if quote in l2:
                    collected.append(l2.split(quote)[0])
                    break
                collected.append(l2)
            return _truncate(" ".join(c.strip() for c in collected))
        return None
    return None


def enrich_codemap(codemap: dict[str, Any], abs_path: str, lines: list[str]) -> None:
    """Add imports, todos, header doc, and per-symbol docs in-place. Zero LLM cost."""
    ext = Path(abs_path).suffix.lower()
    codemap.setdefault("lines", len(lines))

    imports = extract_imports(lines, ext)
    if imports:
        codemap["imports"] = imports

    todos = extract_todos(lines)
    if todos:
        codemap["todos"] = todos

    header = extract_header_doc(lines, ext)
    if header:
        codemap["header_doc"] = header

    for sym in codemap.get("symbols", []):
        line = sym.get("line", 0)
        if line <= 0:
            continue
        doc = extract_doc_above(lines, line)
        if not doc and ext == ".py":
            doc = extract_py_docstring(lines, line)
        if doc:
            sym["doc"] = doc


# ---------- XAML parser (ctags does not support XAML) ----------

XAML_CLASS_RE = re.compile(r'x:Class\s*=\s*"([\w.]+)"')
XAML_NAME_RE = re.compile(r'x:Name\s*=\s*"(\w+)"')
XAML_KEY_RE = re.compile(r'x:Key\s*=\s*"([^"]+)"')
XAML_ROOT_RE = re.compile(r"^\s*<([\w:]+)\b")
XAML_NS_RE = re.compile(r'xmlns(?::(\w+))?\s*=\s*"([^"]+)"')
XAML_EVENT_RE = re.compile(r'\b(Click|Tapped|Loaded|Clicked|Changed|SelectionChanged|TextChanged|Appearing|Disappearing|Command)\s*=\s*"(\w+)"')


def build_codemap_xaml(abs_path: str, lines: list[str]) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    root_element: str | None = None
    x_class: str | None = None
    namespaces: dict[str, str] = {}

    for i, raw in enumerate(lines, start=1):
        stripped = raw.lstrip()
        if root_element is None and stripped.startswith("<") and not stripped.startswith(("<?", "<!--", "<!")):
            m = XAML_ROOT_RE.match(raw)
            if m:
                root_element = m.group(1)
        if x_class is None:
            mc = XAML_CLASS_RE.search(raw)
            if mc:
                x_class = mc.group(1)
                symbols.append({"kind": "xaml_class", "name": x_class, "line": i})
        for m in XAML_NS_RE.finditer(raw):
            prefix = m.group(1) or "default"
            if prefix not in namespaces:
                namespaces[prefix] = m.group(2)
        for m in XAML_NAME_RE.finditer(raw):
            symbols.append({"kind": "xaml_name", "name": m.group(1), "line": i})
        for m in XAML_KEY_RE.finditer(raw):
            symbols.append({"kind": "xaml_resource", "name": m.group(1), "line": i})
        for m in XAML_EVENT_RE.finditer(raw):
            symbols.append({
                "kind": "xaml_handler",
                "name": m.group(2),
                "line": i,
                "sig": f"({m.group(1)})",
            })

    codemap: dict[str, Any] = {
        "path": abs_path,
        "lang": "xaml",
        "source": "xaml-parser",
        "lines": len(lines),
        "root": root_element,
        "symbols": symbols,
    }
    if x_class:
        codemap["code_behind_class"] = x_class
    if namespaces:
        codemap["namespaces"] = namespaces
    if not symbols:
        codemap["preview"] = "".join(lines[:FALLBACK_PREVIEW_LINES])
        codemap["note"] = "No x:Class/x:Name/x:Key found; showing preview."
    return codemap


def compact_codemap(cm: dict[str, Any]) -> dict[str, Any]:
    """Token-efficient transform: group symbols by kind, short-alias hot keys, tag version.

    Goals: eliminate repeated 'kind' values, shorten repeated field names, keep self-documenting
    structure (no mystery integers). Readable schema is in the hook-injected message header.
    """
    syms = cm.pop("symbols", [])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sym in syms:
        kind = sym.pop("kind", "unknown")
        compact = {_KEY_MAP.get(k, k): v for k, v in sym.items()}
        grouped.setdefault(kind, []).append(compact)

    todos = cm.get("todos")
    if todos:
        cm["todos"] = [{"l": t["line"], "k": t["kind"], "t": t["text"]} for t in todos]

    cm["src"] = cm.pop("source", "unknown")
    cm["syms"] = grouped
    cm["v"] = CODEMAP_VERSION
    return cm


def generate_codemap(abs_path: str) -> dict[str, Any] | None:
    """Single disk read; route by extension; enrich; compact."""
    try:
        # utf-8-sig transparently strips the BOM that many .cs/.xaml files from Visual Studio
        # start with. Without this, the first `using` line doesn't match the import regex.
        with open(abs_path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    ext = Path(abs_path).suffix.lower()
    cm: dict[str, Any] | None = None

    if ext in XAML_EXTENSIONS:
        # ctags doesn't parse XAML usefully — route directly.
        cm = build_codemap_xaml(abs_path, lines)
    else:
        if has_ctags():
            entries = run_ctags(abs_path)
            if entries:
                cm = build_codemap_ctags(abs_path, entries)
        if cm is None:
            cm = build_codemap_fallback(abs_path, lines)

    if cm is None:
        return None
    enrich_codemap(cm, abs_path, lines)
    return compact_codemap(cm)


# ---------- cache I/O ----------

def save_codemap(abs_path: str, codemap: dict[str, Any], file_hash: str, file_stat: os.stat_result) -> None:
    ensure_dirs()
    key = cache_key(abs_path)
    map_path = MAPS_DIR / f"{key}.json"
    map_path.write_text(json.dumps(codemap, indent=2), encoding="utf-8")
    index = load_index()
    index[abs_path] = {
        "hash": file_hash,
        "size": file_stat.st_size,
        "mtime": file_stat.st_mtime,
        "map_file": f"maps/{key}.json",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "original_bytes": file_stat.st_size,
    }
    save_index(index)


def load_codemap(entry: dict[str, Any]) -> dict[str, Any] | None:
    map_path = CACHE_DIR / entry.get("map_file", "")
    if not map_path.exists():
        return None
    try:
        cm = json.loads(map_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Reject old-schema maps so PostToolUse regenerates in the current format.
    if cm.get("v") != CODEMAP_VERSION:
        return None
    return cm


# ---------- hook entry points ----------

def read_hook_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def extract_file_path(payload: dict[str, Any]) -> str | None:
    ti = payload.get("tool_input") or {}
    return ti.get("file_path") or ti.get("path")


def is_partial_read(payload: dict[str, Any]) -> bool:
    ti = payload.get("tool_input") or {}
    return ("offset" in ti) or ("limit" in ti)


def should_handle(abs_path: str) -> bool:
    if not os.path.isfile(abs_path):
        return False
    try:
        if os.path.getsize(abs_path) > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    ext = Path(abs_path).suffix.lower()
    # Only cache recognised code files; avoid caching huge JSON/CSV/logs.
    return ext in CODE_EXTENSIONS


def handle_pre() -> None:
    data = read_hook_stdin()
    if data.get("tool_name") != "Read":
        sys.exit(0)
    file_path = extract_file_path(data)
    if not file_path:
        sys.exit(0)
    abs_path = normalize_path(file_path)
    if not should_handle(abs_path):
        sys.exit(0)
    if is_partial_read(data):
        sys.exit(0)  # Claude is asking for a slice -> don't interfere.

    index = load_index()
    entry = index.get(abs_path)
    if not entry:
        sys.exit(0)  # First read -> PostToolUse will cache.
    if not is_cache_valid(abs_path, entry):
        sys.exit(0)  # Stale -> let Read proceed; PostToolUse will refresh.

    codemap = load_codemap(entry)
    if not codemap:
        sys.exit(0)

    # Persist any refreshed mtime/size.
    save_index(index)

    total_lines = codemap.get("lines", "?")
    compact_json = json.dumps(codemap, separators=(",", ":"), ensure_ascii=False)
    message = (
        f"[Claude-Code-Mapper] Cached code map for {abs_path}\n"
        f"Unchanged since {entry.get('generated_at')} (hash {entry.get('hash', '')[:12]}, "
        f"{total_lines} lines).\n"
        f"\n"
        f"STRUCTURAL map — lists symbols with line numbers, signatures, doc-comments, imports, "
        f"and TODOs. Function bodies and control flow are NOT included.\n"
        f"\n"
        f"Schema: syms={{kind:[{{n:name,l:line,s?:sig,d?:doc,e?:end_line,c?:scope,y?:type}}]}}; "
        f"todos=[{{l,k,t}}]; imports=list; src=parser; v=schema version.\n"
        f"\n"
        f"Need logic inside one symbol: Read(file, offset=<l>, limit=<span>)\n"
        f"Need the full file:           Read(file, offset=1, limit={total_lines})  # bypasses cache\n"
        f"\n"
        f"{compact_json}"
    )
    print(message, file=sys.stderr)
    sys.exit(2)  # Block the Read; stderr is surfaced to Claude.


def handle_post() -> None:
    data = read_hook_stdin()
    if data.get("tool_name") != "Read":
        sys.exit(0)
    file_path = extract_file_path(data)
    if not file_path:
        sys.exit(0)
    abs_path = normalize_path(file_path)
    if not should_handle(abs_path):
        sys.exit(0)
    if is_partial_read(data):
        sys.exit(0)

    try:
        file_stat = os.stat(abs_path)
    except OSError:
        sys.exit(0)

    file_hash = sha256_of_file(abs_path)
    index = load_index()
    entry = index.get(abs_path)
    if entry and entry.get("hash") == file_hash:
        # Refresh mtime/size if they drifted, but skip reparse.
        entry["mtime"] = file_stat.st_mtime
        entry["size"] = file_stat.st_size
        save_index(index)
        sys.exit(0)

    codemap = generate_codemap(abs_path)
    if codemap is not None:
        save_codemap(abs_path, codemap, file_hash, file_stat)
    sys.exit(0)


# ---------- CLI helpers ----------

def cmd_status() -> None:
    index = load_index()
    print(f"Cache directory: {CACHE_DIR}")
    print(f"ctags available: {has_ctags()}")
    print(f"Cached files: {len(index)}")
    if not index:
        return
    for path, e in sorted(index.items()):
        size = e.get("size", 0)
        gen = e.get("generated_at", "?")
        print(f"  {path}")
        print(f"    {size:>8} bytes  generated {gen}  hash {e.get('hash', '')[:12]}")


def cmd_clear() -> None:
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"Cleared {CACHE_DIR}")
    else:
        print("No cache directory to clear.")


def cmd_show(path_arg: str) -> None:
    abs_path = normalize_path(path_arg)
    index = load_index()
    entry = index.get(abs_path)
    if not entry:
        print(f"No cached map for {abs_path}", file=sys.stderr)
        sys.exit(1)
    codemap = load_codemap(entry)
    if not codemap:
        print(f"Cache entry exists but map file missing for {abs_path}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(codemap, indent=2))


def cmd_refresh(path_arg: str) -> None:
    """Force-regenerate the map for one file, bypassing cache."""
    abs_path = normalize_path(path_arg)
    if not os.path.isfile(abs_path):
        print(f"Not a file: {abs_path}", file=sys.stderr)
        sys.exit(1)
    file_stat = os.stat(abs_path)
    file_hash = sha256_of_file(abs_path)
    codemap = generate_codemap(abs_path)
    if codemap is None:
        print(f"Failed to generate code map for {abs_path}", file=sys.stderr)
        sys.exit(1)
    save_codemap(abs_path, codemap, file_hash, file_stat)
    symbol_count = sum(len(v) for v in codemap.get("syms", {}).values())
    print(f"Refreshed code map for {abs_path}")
    print(f"  src: {codemap.get('src')}  symbols: {symbol_count}  v: {codemap.get('v')}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "pre":
        handle_pre()
    elif mode == "post":
        handle_post()
    elif mode == "status":
        cmd_status()
    elif mode == "clear":
        cmd_clear()
    elif mode == "show":
        if len(sys.argv) < 3:
            print("Usage: codemap_hook.py show <path>", file=sys.stderr)
            sys.exit(1)
        cmd_show(sys.argv[2])
    elif mode == "refresh":
        if len(sys.argv) < 3:
            print("Usage: codemap_hook.py refresh <path>", file=sys.stderr)
            sys.exit(1)
        cmd_refresh(sys.argv[2])
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
