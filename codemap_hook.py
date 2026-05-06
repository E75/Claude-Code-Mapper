#!/usr/bin/env python3
"""Claude-Code-Mapper: hook-driven, cross-session code-map cache for Claude Code.

Subcommands:
    pre     Run as PreToolUse hook for the Read tool.
    post    Run as PostToolUse hook for the Read tool.
    status  Print cache summary.
    clear   Delete the cache directory.
    show    Print the cached code map for one path.

Cache layout (under CODEMAP_CACHE_DIR, default ~/.claude/codemap-cache):
    index.json              -> { abs_path: { hash, size, mtime, map_file, generated_at } }
    maps/<key>.json         -> code map for that file
    overviews/<hash>.md     -> project_overview.md for that project root
    overviews/<hash>.key    -> git-HEAD cache key for the overview
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
from collections import Counter
from pathlib import Path
from typing import Any

CACHE_DIR = Path(os.environ.get("CODEMAP_CACHE_DIR") or Path.home() / ".claude" / "codemap-cache")
MAPS_DIR = CACHE_DIR / "maps"
OVERVIEW_DIR = CACHE_DIR / "overviews"
INDEX_FILE = CACHE_DIR / "index.json"

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
FALLBACK_PREVIEW_LINES = 40
HASH_CHUNK = 65536

# Minimum line threshold for code-mapping. Files with at most this many
# lines are not cached: the map costs more tokens than it saves, and the
# whole file fits in a single Read anyway. Override via CODEMAP_MIN_LINES.
# Default: 200 — sweet spot where the codemap overhead (~300 framing tokens)
# is more than recouped. Below ~80-120 lines, a direct Read is cheaper.
# CODEMAP_MIN_LINES  — intercept threshold: files LARGER than this get their Read
#   blocked and replaced with the codemap. Smaller files are read normally.
#   Default 200: a 200-line file costs ~2500 tokens; the codemap ~250. Worth it.
try:
    MIN_INTERCEPT_LINES = int(os.environ.get("CODEMAP_MIN_LINES", "200"))
except ValueError:
    MIN_INTERCEPT_LINES = 200

# CODEMAP_MIN_CACHE_LINES — cache threshold: files LARGER than this get their
#   codemap stored by PostToolUse (even if they're below the intercept threshold).
#   Lower than MIN_INTERCEPT_LINES so small/medium files are cached for
#   SessionStart injection without blocking their reads.
#   Default 80: a 90-line file reads fine inline but its codemap in session
#   context still saves tokens on the next session.
try:
    MIN_CACHE_LINES = int(os.environ.get("CODEMAP_MIN_CACHE_LINES", "80"))
except ValueError:
    MIN_CACHE_LINES = 80

# Max symbols in the output map. Prevents huge maps for large files.
# Override via CODEMAP_MAX_SYMBOLS env var.
try:
    MAX_SYMBOLS = int(os.environ.get("CODEMAP_MAX_SYMBOLS", "100"))
except ValueError:
    MAX_SYMBOLS = 100

# TODOs are opt-in — they're rarely relevant and add tokens.
# Enable via CODEMAP_INCLUDE_TODOS=1.
INCLUDE_TODOS = os.environ.get("CODEMAP_INCLUDE_TODOS", "0") == "1"

# SessionStart injection: how many hot files to inject and char budget.
# Override via CODEMAP_SESSION_FILES / CODEMAP_SESSION_MAX_CHARS.
try:
    SESSION_MAX_FILES = int(os.environ.get("CODEMAP_SESSION_FILES", "20"))
except ValueError:
    SESSION_MAX_FILES = 20
try:
    SESSION_MAX_CHARS = int(os.environ.get("CODEMAP_SESSION_MAX_CHARS", "20000"))
except ValueError:
    SESSION_MAX_CHARS = 20000

# Bump when the on-disk map schema changes so old cache entries are invalidated cleanly.
# v1 = original verbose format, v2 = grouped-by-kind with short field aliases.
# v3 = added html/scss/razor parsers, angular ts patterns, path ignores.
CODEMAP_VERSION = 3

# Path substrings (after normalising to forward-slashes + lowercase) that are
# always skipped. Prevents the cache from filling up with vendored/build output
# and translation files that have no code structure.
DEFAULT_IGNORE_SUBSTRINGS = (
    "/node_modules/",
    "/.git/",
    "/dist/",
    "/bin/",
    "/obj/",
    "/.vs/",
    "/packages/",
    "/.angular/",
    "/coverage/",
    "/webdevelopmentgit/pipelines/",
)

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
    # Frontend markup/styles (Angular, statische sites)
    ".html", ".htm", ".scss", ".css", ".sass",
    # .NET / MAUI ecosystem
    ".xaml", ".axaml", ".razor", ".cshtml", ".vb",
}

# Extensions included in the project overview but NOT processed for codemaps.
# .resx = translation/resource files: show in architecture, skip symbol extraction.
OVERVIEW_EXTRA_EXTENSIONS: frozenset[str] = frozenset({
    ".resx", ".xml", ".config", ".csproj", ".json", ".yaml", ".yml",
})

XAML_EXTENSIONS = {".xaml", ".axaml"}
HTML_EXTENSIONS = {".html", ".htm"}
STYLE_EXTENSIONS = {".scss", ".css", ".sass"}
RAZOR_EXTENSIONS = {".cshtml", ".razor"}


# ---------- utilities ----------

def ensure_dirs() -> None:
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    OVERVIEW_DIR.mkdir(parents=True, exist_ok=True)


_WIN_FWDSLASH_RE = re.compile(r'^([A-Za-z]):/(.+)$')


def _norm_index_key(path: str) -> str:
    """On Windows, normalise forward-slash drive paths to backslash so that
    C:/foo and C:\\foo always resolve to the same index key."""
    if os.name == "nt":
        m = _WIN_FWDSLASH_RE.match(path)
        if m:
            return f"{m.group(1).upper()}:\\{m.group(2).replace('/', '\\')}"
    return path


def load_index() -> dict[str, Any]:
    if not INDEX_FILE.exists():
        return {}
    try:
        raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if os.name == "nt":
            # Normalise any stale forward-slash keys written by earlier versions.
            raw = {_norm_index_key(k): v for k, v in raw.items()}
        return raw
    except (json.JSONDecodeError, OSError):
        return {}


def save_index(index: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    # Compact JSON: ~35% smaller than indent=2, faster to read/write.
    tmp.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def find_git_root(start: Path) -> Path:
    """Walk up from start to find the nearest .git directory."""
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return start.resolve()


def normalize_path(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


def _path_for_ignore_match(abs_path: str) -> str:
    """Normalise to forward-slash + lowercase for substring-ignore matching."""
    return abs_path.replace("\\", "/").lower()


def is_ignored(abs_path: str) -> bool:
    """Check whether the path is skipped by defaults or CODEMAP_IGNORE_PATHS.

    CODEMAP_IGNORE_PATHS = ';'-separated substrings, case-insensitive.
    Example: setx CODEMAP_IGNORE_PATHS "/legacy/;/generated/"
    """
    needle = _path_for_ignore_match(abs_path)
    for s in DEFAULT_IGNORE_SUBSTRINGS:
        if s in needle:
            return True
    extra = os.environ.get("CODEMAP_IGNORE_PATHS", "")
    if extra:
        for raw in extra.split(";"):
            pat = raw.strip().replace("\\", "/").lower()
            if pat and pat in needle:
                return True
    return False


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

_CTAGS_AVAILABLE: bool | None = None


def has_ctags() -> bool:
    global _CTAGS_AVAILABLE
    if _CTAGS_AVAILABLE is None:
        try:
            subprocess.run(["ctags", "--version"], capture_output=True, timeout=5)
            _CTAGS_AVAILABLE = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            _CTAGS_AVAILABLE = False
    return _CTAGS_AVAILABLE


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
    # Angular @Input()/@Output() inline property decorators must come before
    # ts_decorator, otherwise the generic decorator match swallows them.
    ("ts_input", re.compile(
        r"^\s+@Input\s*\([^)]*\)\s+"
        r"(?:(?:public|private|protected|readonly|static|override)\s+)*"
        r"(\w+)"
    )),
    ("ts_output", re.compile(
        r"^\s+@Output\s*\([^)]*\)\s+"
        r"(?:(?:public|private|protected|readonly|static|override)\s+)*"
        r"(\w+)"
    )),
    # Modern Angular DI via inject(): `private foo = inject(Foo)` or typed
    # variant `private foo: FooService = inject(FooService)`.
    ("ts_inject", re.compile(
        r"^\s+(?:public\s+|private\s+|protected\s+|readonly\s+)+\s*"
        r"(\w+)\s*(?::\s*[^=]+?)?\s*=\s*inject\s*\(\s*(\w+)\s*\)"
    )),
    # TS decorator (on its own line above a class/method)
    ("ts_decorator", re.compile(r"^\s*@([A-Z]\w*)\s*\(")),
    # TS/JS class method (indented, inside a class). Conservative: requires return-type or body brace.
    # Negative lookahead filters out control-flow keywords that would otherwise be matched as a method
    # (e.g. `if (x) {` inside a method body).
    ("ts_method", re.compile(
        r"^\s{2,}(?:(?:public|private|protected|static|async|readonly|override|abstract)\s+)*"
        r"(?:async\s+)?"
        r"(?!(?:if|for|while|switch|return|throw|catch|do|else|try|await|yield|new|typeof|void|delete|break|continue|default|case)\b)"
        r"(\w+)\s*(?:<[^>]{1,60}>)?\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\]|&\s,.?]+)?\s*[\{;]"
    )),
    ("go_func", re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)")),
    ("gd_func", re.compile(r"^\s*(?:static\s+)?func\s+(\w+)\s*\(([^)]*)\)")),
    ("gd_class", re.compile(r"^\s*class_name\s+(\w+)")),
    ("rust_fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)")),
    # VB.NET: type declarations (Class/Module/Interface/Structure/Enum).
    ("vb_type", re.compile(
        r"^\s*(?:(?:Public|Private|Protected|Friend|Partial|NotInheritable|MustInherit)\s+)*"
        r"(?:Class|Module|Interface|Structure|Enum)\s+(\w+)",
        re.IGNORECASE,
    )),
    # VB.NET: members (Sub/Function/Property) met modifier-prefix.
    ("vb_member", re.compile(
        r"^\s+(?:(?:Public|Private|Protected|Friend|Shared|Overrides|Overridable|"
        r"MustOverride|NotOverridable|Overloads|ReadOnly|WriteOnly|Default|Async|Iterator)\s+)+"
        r"(?:Sub|Function|Property)\s+(\w+)",
        re.IGNORECASE,
    )),
    # SQL DDL: CREATE/ALTER PROCEDURE/FUNCTION/TABLE/VIEW/INDEX/TRIGGER [schema.]name.
    ("sql_object", re.compile(
        r"^\s*(?:CREATE|ALTER)\s+(?:OR\s+REPLACE\s+)?"
        r"(?:TABLE|VIEW|PROCEDURE|FUNCTION|INDEX|TRIGGER|TYPE|SCHEMA)\s+"
        r"(?:\[?(?:dbo|[A-Za-z_][\w]*)\]?\.)?\[?(\w+)\]?",
        re.IGNORECASE,
    )),
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
        ".vb": ("vb_",),
        ".sql": ("sql_",),
    }
    all_prefixes = {"gd_", "go_", "rust_", "py_", "cs_", "ts_", "vb_", "sql_"}
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
IMPORT_RE_VB = re.compile(r"^\s*Imports\s+([\w.]+)\s*$", re.IGNORECASE)
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
            if len(out) >= 15:
                break
        return out
    if ext == ".py":
        pat = IMPORT_RE_PY
        groups = (1, 2)
    elif ext in {".cs", ".razor", ".cshtml"}:
        pat = IMPORT_RE_CS
        groups = (1,)
    elif ext == ".vb":
        pat = IMPORT_RE_VB
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
        if len(out) >= 15:
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

    if INCLUDE_TODOS:
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


# ---------- HTML (Angular templates and static HTML) ----------

# Custom components (kebab-case: prefix-name), plus common Angular/Material/CDK tags.
HTML_COMPONENT_RE = re.compile(
    r'<([a-z][a-z0-9]*(?:-[a-z0-9]+)+|ng-[a-z-]+|router-outlet|mat-[a-z-]+|cdk-[a-z-]+)\b'
)
# Event binding: (click)="handler(...)" — handler name + expression.
HTML_EVENT_RE = re.compile(r'\((\w+)\)\s*=\s*"([^"]{0,160})"')
# Two-way: [(ngModel)]="prop"
HTML_TWOWAY_RE = re.compile(r'\[\((\w+)\)\]\s*=\s*"([^"]{0,160})"')
# Structural directives (legacy): *ngIf / *ngFor / *ngTemplateOutlet
HTML_STRUCT_RE = re.compile(r'\*(ng[A-Z]\w+)\s*=\s*"([^"]{0,160})"')
# Modern control flow: @if, @for (x of list), @switch, etc.
HTML_CTRL_RE = re.compile(r'^\s*@(if|for|switch|else|empty|case|default|defer)\b[^\n{]*')
# Template reference variables: <input #myRef ...>
HTML_TMPL_REF_RE = re.compile(r'#(\w+)(?=\s|=|/?>)')


def build_codemap_html(abs_path: str, lines: list[str]) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, name: str, line: int, sig: str | None = None) -> None:
        key = (kind, name)
        if key in seen:
            return
        seen.add(key)
        sym: dict[str, Any] = {"kind": kind, "name": name, "line": line}
        if sig:
            sym["sig"] = sig[:80]
        symbols.append(sym)

    for i, raw in enumerate(lines, start=1):
        # Sla commentaarregels over.
        stripped = raw.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        for m in HTML_COMPONENT_RE.finditer(raw):
            add("component", m.group(1), i)
        for m in HTML_EVENT_RE.finditer(raw):
            add("event", m.group(1), i, f"=> {m.group(2).strip()}")
        for m in HTML_TWOWAY_RE.finditer(raw):
            add("twoway", m.group(1), i, f"<-> {m.group(2).strip()}")
        for m in HTML_STRUCT_RE.finditer(raw):
            add("directive", m.group(1), i, m.group(2).strip())
        m = HTML_CTRL_RE.match(raw)
        if m:
            add("control", m.group(1), i, stripped[:80])
        for m in HTML_TMPL_REF_RE.finditer(raw):
            add("tmplref", m.group(1), i)

    codemap: dict[str, Any] = {
        "path": abs_path,
        "lang": "html",
        "source": "html-parser",
        "lines": len(lines),
        "symbols": symbols,
    }
    if not symbols:
        codemap["preview"] = "".join(lines[:FALLBACK_PREVIEW_LINES])
        codemap["note"] = "No Angular-style bindings/components found; showing preview."
    return codemap


# ---------- SCSS / CSS ----------

SCSS_USE_RE = re.compile(r'^\s*@(use|import|forward)\s+[\'"]([^\'"]+)[\'"]')
SCSS_MIXIN_RE = re.compile(r'^\s*@mixin\s+([\w-]+)')
SCSS_FUNCTION_RE = re.compile(r'^\s*@function\s+([\w-]+)')
SCSS_VAR_RE = re.compile(r'^\s*\$([\w-]+)\s*:')
SCSS_KEYFRAMES_RE = re.compile(r'^\s*@keyframes\s+([\w-]+)')
SCSS_MEDIA_RE = re.compile(r'^\s*@media\b\s*(.+?)\s*\{')
# Top-level selector on a single line: `html, body {`. Excludes @-rules.
SCSS_TOP_SELECTOR_RE = re.compile(r'^([^\s@/].*?)\s*\{\s*$')
# Top-level selector where `{` lives on the next line (CSS/SCSS style with
# brace on a new line). Scans the whole file via re.MULTILINE.
SCSS_MULTILINE_SELECTOR_RE = re.compile(
    r'^([^\s@/][^\n{}]{0,200}?)\s*\n\s*\{',
    re.MULTILINE,
)


def build_codemap_scss(abs_path: str, lines: list[str]) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    imports: list[str] = []
    seen_imports: set[str] = set()
    seen_selectors: set[tuple[int, str]] = set()  # (line, name) dedupe
    ext = Path(abs_path).suffix.lower()

    for i, raw in enumerate(lines, start=1):
        m = SCSS_USE_RE.match(raw)
        if m:
            target = m.group(2)
            if target not in seen_imports:
                seen_imports.add(target)
                imports.append(target)
            continue
        m = SCSS_MIXIN_RE.match(raw)
        if m:
            symbols.append({"kind": "mixin", "name": m.group(1), "line": i})
            continue
        m = SCSS_FUNCTION_RE.match(raw)
        if m:
            symbols.append({"kind": "function", "name": m.group(1), "line": i})
            continue
        m = SCSS_KEYFRAMES_RE.match(raw)
        if m:
            symbols.append({"kind": "keyframes", "name": m.group(1), "line": i})
            continue
        m = SCSS_VAR_RE.match(raw)
        if m:
            symbols.append({"kind": "variable", "name": m.group(1), "line": i})
            continue
        m = SCSS_MEDIA_RE.match(raw)
        if m:
            symbols.append({"kind": "media", "name": m.group(1).strip()[:60], "line": i})
            continue
        m = SCSS_TOP_SELECTOR_RE.match(raw)
        if m:
            sel = m.group(1).strip()
            if sel and len(sel) <= 140:
                key = (i, sel)
                if key not in seen_selectors:
                    seen_selectors.add(key)
                    symbols.append({"kind": "selector", "name": sel, "line": i})

    # Second pass: top-level selectors where `{` sits on the next line
    # (common CSS style in Map.css/Reset.css). Operates on the full content.
    content = "".join(lines)
    for m in SCSS_MULTILINE_SELECTOR_RE.finditer(content):
        sel = m.group(1).strip().lstrip("\ufeff")
        if not sel or len(sel) > 140:
            continue
        # Skip if this happened to be a declaration (contains `:` without a pseudo signal).
        # Real selectors: class/id/tag/pseudo-combinator. A `;` in the "selector" indicates
        # a false hit (property list).
        if ";" in sel:
            continue
        line_no = content.count("\n", 0, m.start()) + 1
        key = (line_no, sel)
        if key not in seen_selectors:
            seen_selectors.add(key)
            symbols.append({"kind": "selector", "name": sel, "line": line_no})

    symbols.sort(key=lambda s: s.get("line", 0))

    codemap: dict[str, Any] = {
        "path": abs_path,
        "lang": ext.lstrip(".") or "scss",
        "source": "scss-parser",
        "lines": len(lines),
        "symbols": symbols,
    }
    if imports:
        codemap["imports"] = imports
    if not symbols:
        codemap["preview"] = "".join(lines[:FALLBACK_PREVIEW_LINES])
    return codemap


# ---------- Razor (.cshtml / .razor) ----------

RAZOR_MODEL_RE = re.compile(r'^\s*@model\s+(.+?)\s*$')
RAZOR_USING_RE = re.compile(r'^\s*@using\s+(.+?)\s*$')
RAZOR_INJECT_RE = re.compile(r'^\s*@inject\s+(\S+)\s+(\w+)')
RAZOR_PAGE_RE = re.compile(r'^\s*@page(?:\s+"(.+?)")?\s*$')
RAZOR_SECTION_RE = re.compile(r'^\s*@section\s+(\w+)')
RAZOR_LAYOUT_RE = re.compile(r'Layout\s*=\s*"([^"]+)"')
RAZOR_PARTIAL_RE = re.compile(r'@(?:await\s+)?Html\.(?:Partial|PartialAsync|RenderPartial|RenderPartialAsync)\s*\(\s*"([^"]+)"')


def build_codemap_razor(abs_path: str, lines: list[str]) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    imports: list[str] = []
    seen_imports: set[str] = set()

    def add_import(s: str) -> None:
        if s and s not in seen_imports:
            seen_imports.add(s)
            imports.append(s)

    for i, raw in enumerate(lines, start=1):
        m = RAZOR_MODEL_RE.match(raw)
        if m:
            symbols.append({"kind": "model", "name": m.group(1).strip(), "line": i})
            continue
        m = RAZOR_PAGE_RE.match(raw)
        if m:
            symbols.append({"kind": "page", "name": (m.group(1) or "").strip() or "(root)", "line": i})
            continue
        m = RAZOR_USING_RE.match(raw)
        if m:
            add_import(m.group(1).strip().rstrip(";"))
            continue
        m = RAZOR_INJECT_RE.match(raw)
        if m:
            symbols.append({"kind": "inject", "name": m.group(2), "line": i, "sig": m.group(1)})
            continue
        m = RAZOR_SECTION_RE.match(raw)
        if m:
            symbols.append({"kind": "section", "name": m.group(1), "line": i})
            continue
        m = RAZOR_LAYOUT_RE.search(raw)
        if m:
            symbols.append({"kind": "layout", "name": m.group(1), "line": i})
        for pm in RAZOR_PARTIAL_RE.finditer(raw):
            symbols.append({"kind": "partial", "name": pm.group(1), "line": i})

    # Also run a fallback scan for C# blocks inside the cshtml (@{ ... } or @functions).
    # ctags doesn't pick this up; we reuse the fallback regex on the whole file for
    # any methods/classes declared in code blocks.
    try:
        extra = build_codemap_fallback(abs_path, lines).get("symbols", [])
    except Exception:
        extra = []
    # Filter so we don't double-count: skip kinds already collected above.
    existing = {(s["kind"], s.get("name")) for s in symbols}
    for s in extra:
        if (s["kind"], s.get("name")) not in existing:
            symbols.append(s)

    codemap: dict[str, Any] = {
        "path": abs_path,
        "lang": "razor",
        "source": "razor-parser",
        "lines": len(lines),
        "symbols": symbols,
    }
    if imports:
        codemap["imports"] = imports
    if not symbols:
        codemap["preview"] = "".join(lines[:FALLBACK_PREVIEW_LINES])
    return codemap


def compact_codemap(cm: dict[str, Any]) -> dict[str, Any]:
    """Token-efficient transform: group symbols by kind, short-alias hot keys, tag version."""
    syms = cm.pop("symbols", [])
    if len(syms) > MAX_SYMBOLS:
        syms = syms[:MAX_SYMBOLS]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sym in syms:
        kind = sym.pop("kind", "unknown")
        compact = {_KEY_MAP.get(k, k): v for k, v in sym.items()}
        grouped.setdefault(kind, []).append(compact)

    todos = cm.get("todos")
    if todos:
        cm["todos"] = [{"l": t["line"], "k": t["kind"], "t": t["text"]} for t in todos]

    cm.pop("source", None)  # not useful to Claude
    cm["syms"] = grouped
    cm["v"] = CODEMAP_VERSION
    return cm


def generate_codemap(abs_path: str, _lines: list[str] | None = None) -> dict[str, Any] | None:
    """Route by extension; enrich; compact.

    Pass _lines to reuse already-read content and avoid a second disk read.
    """
    if _lines is None:
        try:
            # utf-8-sig strips the BOM that many .cs/.xaml files from Visual Studio carry.
            with open(abs_path, "r", encoding="utf-8-sig", errors="replace") as f:
                _lines = f.readlines()
        except OSError:
            return None
    lines = _lines

    ext = Path(abs_path).suffix.lower()
    cm: dict[str, Any] | None = None

    # ctags is generic but doesn't understand XAML/HTML/SCSS/Razor — route those
    # specials first, then ctags, then the fallback regex.
    if ext in XAML_EXTENSIONS:
        cm = build_codemap_xaml(abs_path, lines)
    elif ext in HTML_EXTENSIONS:
        cm = build_codemap_html(abs_path, lines)
    elif ext in STYLE_EXTENSIONS:
        cm = build_codemap_scss(abs_path, lines)
    elif ext in RAZOR_EXTENSIONS:
        cm = build_codemap_razor(abs_path, lines)
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
    # Compact JSON: smaller on disk, faster to load. ensure_ascii=False keeps
    # non-ASCII identifiers readable without escape overhead.
    map_path.write_text(json.dumps(codemap, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    index = load_index()
    index[abs_path] = {
        "hash": file_hash,
        "size": file_stat.st_size,
        "mtime": file_stat.st_mtime,
        "map_file": f"maps/{key}.json",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lines": codemap.get("lines"),  # stored to avoid loading the map just for a threshold check
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
    if is_ignored(abs_path):
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
    codemap: dict[str, Any] | None = None

    if entry:
        if is_cache_valid(abs_path, entry):
            # Use line count stored in the index entry to avoid loading the map
            # file just to find out the file is below the intercept threshold.
            cached_lines = entry.get("lines")
            if isinstance(cached_lines, int) and cached_lines <= MIN_INTERCEPT_LINES:
                sys.exit(0)

            codemap = load_codemap(entry)
            if codemap is None:
                sys.exit(0)

            # Old index entries (pre-"lines" field) need the fallback check.
            if cached_lines is None:
                cached_lines = codemap.get("lines")
                if isinstance(cached_lines, int) and cached_lines <= MIN_INTERCEPT_LINES:
                    sys.exit(0)

            # Track usage so SessionStart can inject the hottest files.
            entry["access_count"] = entry.get("access_count", 0) + 1
            entry["last_accessed"] = time.time()
            save_index(index)

        # entry existed but is stale (file changed) → warn Claude, then let Read
        # proceed normally; PostToolUse will regenerate the cache afterwards.
        if codemap is None:
            try:
                fname = Path(abs_path).name
                print(
                    f"[CodeMap] ⚠ {fname} changed since last cache — reading full file. "
                    "Codemap will be updated after this read.",
                    file=sys.stderr,
                )
            except Exception:
                pass
            sys.exit(0)
    else:
        # Not cached. For very large files that would be blocked by read_large_file_guard
        # the PostToolUse hook never fires, so generate the codemap here on-the-fly.
        # Smaller files pass through and get cached by PostToolUse instead.
        try:
            line_threshold = int(os.environ.get("CLAUDE_READ_GUARD_THRESHOLD", "1500"))
        except ValueError:
            line_threshold = 1500
        try:
            # Single read: decode to text lines for both line-count check and parsing.
            # This avoids the binary-read-then-text-read double I/O of the previous approach.
            with open(abs_path, "r", encoding="utf-8-sig", errors="replace") as f:
                pre_lines = f.readlines()
        except OSError:
            sys.exit(0)
        if len(pre_lines) < line_threshold:
            sys.exit(0)
        try:
            file_stat = os.stat(abs_path)
        except OSError:
            sys.exit(0)
        file_hash = sha256_of_file(abs_path)
        codemap = generate_codemap(abs_path, _lines=pre_lines)  # reuses pre-read lines
        if codemap is None:
            sys.exit(0)
        save_codemap(abs_path, codemap, file_hash, file_stat)
        entry = {
            "hash": file_hash,
            "size": file_stat.st_size,
            "mtime": file_stat.st_mtime,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    total_lines = codemap.get("lines", "?")
    compact_json = json.dumps(codemap, separators=(",", ":"), ensure_ascii=False)
    message = (
        f"[CodeMap v{CODEMAP_VERSION}] {abs_path} ({total_lines}L) — use Read(offset=N,limit=M) for a section\n"
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

    index = load_index()
    entry = index.get(abs_path)

    if entry:
        # is_cache_valid returns True in two cases:
        #   A) mtime+size match (fast-path) → entry unmodified, no write needed.
        #   B) hash matches but mtime/size drifted → entry updated in-place, must write.
        old_size, old_mtime = entry.get("size"), entry.get("mtime")
        if is_cache_valid(abs_path, entry):
            # Only persist when is_cache_valid actually updated the entry (case B).
            if entry.get("size") != old_size or entry.get("mtime") != old_mtime:
                save_index(index)
            sys.exit(0)

    try:
        file_stat = os.stat(abs_path)
    except OSError:
        sys.exit(0)

    file_hash = sha256_of_file(abs_path)
    if entry and entry.get("hash") == file_hash:
        # Content unchanged but mtime/size drifted (e.g. git checkout).
        entry["mtime"] = file_stat.st_mtime
        entry["size"] = file_stat.st_size
        save_index(index)
        sys.exit(0)

    codemap = generate_codemap(abs_path)
    if codemap is not None:
        cached_lines = codemap.get("lines")
        if isinstance(cached_lines, int) and cached_lines < MIN_CACHE_LINES:
            sys.exit(0)  # Below cache threshold — too small to be worth caching.
        save_codemap(abs_path, codemap, file_hash, file_stat)
    sys.exit(0)


# ---------- index maintenance ----------

_CLEANUP_SIDECAR = CACHE_DIR / "_last_cleanup"
_CLEANUP_INTERVAL = 86400  # run at most once per 24h


def _should_run_cleanup() -> bool:
    try:
        if not _CLEANUP_SIDECAR.exists():
            return True
        return time.time() - _CLEANUP_SIDECAR.stat().st_mtime > _CLEANUP_INTERVAL
    except OSError:
        return True


def _mark_cleanup_done() -> None:
    try:
        ensure_dirs()
        _CLEANUP_SIDECAR.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def run_index_cleanup(index: dict[str, Any]) -> bool:
    """Remove stale entries from the index (in-place). Returns True if anything was removed.

    Pruning rules:
    - File no longer exists on disk.
    - File not accessed in 60+ days AND access_count < 5  (cold entries).
    Also removes orphaned map files (maps/ entries not referenced in index).
    """
    cutoff = time.time() - 60 * 86400
    to_remove: list[str] = []
    for path, entry in index.items():
        if not os.path.isfile(path):
            to_remove.append(path)
            continue
        last = entry.get("last_accessed", 0.0)
        count = entry.get("access_count", 0)
        if last and last < cutoff and count < 5:
            to_remove.append(path)

    if not to_remove:
        return False

    live_map_files: set[str] = set()
    for path in to_remove:
        entry = index.pop(path)
        mf = entry.get("map_file", "")
        if mf:
            # Only delete if no other entry references this map file.
            live_map_files.add(mf)

    # Remove orphaned map files.
    for mf in live_map_files:
        mp = CACHE_DIR / mf
        try:
            if mp.exists():
                mp.unlink()
        except OSError:
            pass

    return True


# ---------- project overview ----------

# File extensions used inside OVERVIEW_DIR (named by scope-root hash).
OVERVIEW_MD_EXT = ".md"
OVERVIEW_KEY_EXT = ".key"

# Dirs skipped when walking for the overview.
_OVERVIEW_IGNORE: frozenset[str] = frozenset({
    "node_modules", ".git", "dist", "bin", "obj", ".vs", ".vscode",
    "packages", ".angular", "coverage", ".next", ".nuxt", "build",
    "out", "target", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".idea", ".gradle", ".venv", "venv", "env",
    ".claude-cache", ".claude-index", "TestResults", "publish",
})

# (filename or suffix → label). Checked top-level + 1 dir deep.
_TECH_MARKERS: list[tuple[str, str]] = [
    ("angular.json",          "Angular"),
    ("nx.json",               "Angular / Nx"),
    (".csproj",               ".NET / C#"),
    (".sln",                  ".NET Solution"),
    (".vbproj",               ".NET / VB"),
    (".fsproj",               ".NET / F#"),
    ("go.mod",                "Go"),
    ("pom.xml",               "Java / Maven"),
    ("build.gradle",          "Java / Gradle"),
    ("Cargo.toml",            "Rust"),
    ("pyproject.toml",        "Python"),
    ("requirements.txt",      "Python"),
    ("Dockerfile",            "Docker"),
    ("docker-compose.yml",    "Docker Compose"),
    ("docker-compose.yaml",   "Docker Compose"),
    (".sql",                  "SQL"),
    (".razor",                "ASP.NET Razor"),
    (".cshtml",               "ASP.NET Razor"),
    ("package.json",          "Node.js"),
]


def _overview_git_key(git_root: Path) -> str:
    """Fast cache-key: stat .git/HEAD + .git/index."""
    git_dir = git_root / ".git"
    parts: list[str] = []
    for name in ("HEAD", "index"):
        p = git_dir / name
        try:
            parts.append(f"{name}:{p.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{name}:missing")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _detect_tech_stack(root: Path) -> list[str]:
    """Return detected technology labels, ordered by priority."""
    found: dict[str, int] = {}   # label → priority (lower = higher prio)
    dirs_to_scan = [root]
    try:
        dirs_to_scan += [
            d for d in root.iterdir()
            if d.is_dir() and d.name not in _OVERVIEW_IGNORE and not d.name.startswith(".")
        ]
    except OSError:
        pass
    for d in dirs_to_scan[:40]:
        try:
            for f in d.iterdir():
                if not f.is_file():
                    continue
                for i, (marker, label) in enumerate(_TECH_MARKERS):
                    if label in found:
                        continue
                    if marker.startswith("."):          # extension match
                        if f.suffix.lower() == marker:
                            found[label] = i
                    else:                               # exact filename match
                        if f.name == marker or f.name.lower() == marker.lower():
                            found[label] = i
        except OSError:
            pass
    # If Angular is found, remove bare Node.js label.
    if "Angular" in found or "Angular / Nx" in found:
        found.pop("Node.js", None)
    return [label for label, _ in sorted(found.items(), key=lambda x: x[1])]


def _collect_code_files(root: Path, cap: int = 400) -> list[tuple[Path, int]]:
    """Walk root; return (path, line_count) for code + overview-extra files, sorted large-first.

    Uses CODE_EXTENSIONS ∪ OVERVIEW_EXTRA_EXTENSIONS so that e.g. .resx files appear
    in the project overview even though they are not processed for codemaps.
    Caps at `cap` files to stay fast on huge repos.
    """
    all_exts = CODE_EXTENSIONS | OVERVIEW_EXTRA_EXTENSIONS
    result: list[tuple[Path, int]] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _OVERVIEW_IGNORE and not d.startswith(".")
        ]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix.lower() not in all_exts:
                continue
            try:
                with open(p, "rb") as fh:
                    lc = sum(1 for _ in fh)
                result.append((p, lc))
            except OSError:
                continue
        if len(result) >= cap:
            break
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def _build_dir_tree(root: Path) -> str:
    """Compact indented directory tree with filenames.

    Parameters scale automatically with project size:
      Small  (≤60 files):  max_depth=5, max_lines=120, files_per_dir=all
      Medium (≤200 files): max_depth=4, max_lines=100, files_per_dir=8
      Large  (>200 files): max_depth=3, max_lines=80,  files_per_dir=4

    At max_depth the tree never hard-collapses to a single '... (N dirs, M files)'
    line.  Instead it always shows the files present at that level, then lists
    sub-directory names (with their direct file count) without recursing further.
    This means the user always sees concrete filenames, never an opaque counter.
    """
    lines: list[str] = [f"{root.name}/"]
    dir_info: dict[str, tuple[list[str], list[str]]] = {}  # abs-path → (subdirs, filenames)
    total_files = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(
            [d for d in dirnames if d not in _OVERVIEW_IGNORE and not d.startswith(".")],
            key=str.lower,
        )
        dir_info[dirpath] = (list(dirnames), sorted(filenames, key=str.lower))
        total_files += len(filenames)

    if total_files <= 60:
        max_depth, max_lines, files_per_dir = 5, 120, 999
    elif total_files <= 200:
        max_depth, max_lines, files_per_dir = 4, 100, 8
    else:
        max_depth, max_lines, files_per_dir = 3, 80, 4

    def render(dp: str, depth: int, prefix: str) -> None:
        if len(lines) >= max_lines:
            return
        subs, fnames = dir_info.get(dp, ([], []))

        # Always show the files at this level (up to files_per_dir).
        shown = fnames[:files_per_dir]
        hidden = len(fnames) - files_per_dir if len(fnames) > files_per_dir else 0
        for fname in shown:
            lines.append(f"{prefix}{fname}")
        if hidden > 0:
            lines.append(f"{prefix}... (+{hidden} files)")

        if depth >= max_depth:
            # At the depth limit: list sub-directory names + their direct file
            # count, but do NOT recurse.  This replaces the old opaque
            # '└─ ... (N dirs, M files)' line with something actually useful.
            for i, sub in enumerate(subs):
                if len(lines) >= max_lines:
                    break
                connector = "└─" if i == len(subs) - 1 else "├─"
                sub_abs = os.path.join(dp, sub)
                sub_subs, sub_fnames = dir_info.get(sub_abs, ([], []))
                deeper = "+" if sub_subs else ""
                label = f"  [{len(sub_fnames)}{deeper}f]" if sub_fnames or sub_subs else ""
                lines.append(f"{prefix}{connector} {sub}/{label}")
            return

        for i, sub in enumerate(subs):
            if len(lines) >= max_lines:
                return
            connector = "└─" if i == len(subs) - 1 else "├─"
            sub_abs = os.path.join(dp, sub)
            sub_subs, sub_fnames = dir_info.get(sub_abs, ([], []))
            # Show [Nf] only for leaf dirs (no sub-subdirs) so there's no
            # redundancy with what render() will print on the next call.
            fcount_label = f"  [{len(sub_fnames)}f]" if not sub_subs else ""
            lines.append(f"{prefix}{connector} {sub}/{fcount_label}")
            child_prefix = prefix + ("   " if i == len(subs) - 1 else "│  ")
            render(sub_abs, depth + 1, child_prefix)

    render(str(root), 1, "")
    return "\n".join(lines[:max_lines])


def generate_project_overview(scope_root: Path, git_root: Path) -> str:
    """Build a compact Markdown overview of the project.

    Sections:
      1. Directory structure (depth 3, dirs-only with file counts)
      2. All code files sorted by size (top 30, full list if ≤30)
      3. Tech stack
      4. Summary stats
    """
    tech = _detect_tech_stack(scope_root)
    code_files = _collect_code_files(scope_root)
    tree = _build_dir_tree(scope_root)

    total_files = len(code_files)
    total_lines = sum(lc for _, lc in code_files)
    scope_label = "sub-project" if scope_root.resolve() != git_root.resolve() else "project"

    parts: list[str] = [
        f"# Project Overview ({scope_label}): {scope_root.name}",
        f"Root: `{scope_root}`",
        f"_Auto-generated by codemap_hook. Updates on git HEAD change._",
        "",
        "## Tech Stack",
    ]
    if tech:
        parts += [f"- {t}" for t in tech]
    else:
        parts.append("_(not detected)_")

    parts += [
        "",
        "## Directory Structure",
        "```",
        tree,
        "```",
        "",
        f"## Code Files by Size",
        "| File | Lines |",
        "|------|------:|",
    ]
    show_n = min(30, total_files)
    for p, lc in code_files[:show_n]:
        try:
            rel = p.relative_to(scope_root)
        except ValueError:
            rel = p
        parts.append(f"| `{rel}` | {lc:,} |")
    if total_files > show_n:
        parts.append(f"| _(+{total_files - show_n} smaller files)_ | |")

    ext_counts = Counter(p.suffix.lower() for p, _ in code_files)
    top_exts = ", ".join(f"{ext}({n})" for ext, n in ext_counts.most_common(6))

    parts += [
        "",
        "## Summary",
        f"- **{total_files}** code files · **{total_lines:,}** total lines",
        f"- Extensions: {top_exts}",
    ]
    if code_files:
        biggest_p, biggest_lc = code_files[0]
        try:
            brel = biggest_p.relative_to(scope_root)
        except ValueError:
            brel = biggest_p
        pct = round(biggest_lc / total_lines * 100) if total_lines else 0
        parts.append(f"- Largest: `{brel}` ({biggest_lc:,}L = {pct}% of codebase)")

    return "\n".join(parts)


def _overview_cache_paths(scope_root: Path) -> tuple[Path, Path]:
    """Return (key_file, md_file) in the central overviews cache dir.

    Files are named by a hash of the scope root path so different projects
    never collide and the project directory itself stays clean.
    """
    h = cache_key(str(scope_root))
    return OVERVIEW_DIR / f"{h}{OVERVIEW_KEY_EXT}", OVERVIEW_DIR / f"{h}{OVERVIEW_MD_EXT}"


def _load_cached_overview(scope_root: Path, expected_key: str) -> str | None:
    key_file, md_file = _overview_cache_paths(scope_root)
    if not key_file.is_file() or not md_file.is_file():
        return None
    try:
        if key_file.read_text(encoding="utf-8").strip() != expected_key:
            return None
        return md_file.read_text(encoding="utf-8")
    except OSError:
        return None


def _save_overview(scope_root: Path, key: str, content: str) -> None:
    key_file, md_file = _overview_cache_paths(scope_root)
    try:
        OVERVIEW_DIR.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
        md_file.write_text(content, encoding="utf-8")
    except OSError:
        pass


def get_or_build_overview(scope_root: Path, git_root: Path) -> str:
    """Return cached overview if still valid; else rebuild, save, and return it."""
    key = _overview_git_key(git_root)
    cached = _load_cached_overview(scope_root, key)
    if cached:
        return cached
    content = generate_project_overview(scope_root, git_root)
    _save_overview(scope_root, key, content)
    return content


# ---------- SessionStart injection ----------

def cmd_session() -> None:
    """SessionStart hook: inject codemaps of hot files to avoid re-reading them each session.

    Reads stdin JSON (hookEventName + cwd). Scores cached files by access frequency
    and recency, then emits the top N as additionalContext. Files are scoped to the
    current project's git root so cross-project noise is excluded.

    Scoring: access_count (raw hits) + recency bonus (10/5/2/0 for <1d/<7d/<30d/older).
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    cwd = data.get("cwd") or os.getcwd()

    # Skip when working in the Claude config dir itself.
    try:
        cwd_path = Path(cwd).resolve()
        home_claude = Path.home() / ".claude"
        if cwd_path == home_claude.resolve() or cwd_path == Path.home().resolve():
            return
    except Exception:
        return

    # Scope to files under the current working directory (the specific project).
    git_root: Path | None = None
    project_prefix = cwd.replace("\\", "/").lower()
    try:
        git_root = find_git_root(Path(cwd))
    except Exception:
        pass

    # Determine the scope root (sub-project vs. repo root).
    scope_root = Path(cwd).resolve()
    if git_root:
        # Use cwd as scope root if it looks like a sub-project, else git root.
        cwd_resolved = Path(cwd).resolve()
        scope_root = cwd_resolved if cwd_resolved != git_root else git_root

    project_name = scope_root.name
    # Filter codemaps to only files under the scope root (not the whole git repo).
    project_prefix = str(scope_root).replace("\\", "/").lower()

    # Collect git-modified files: norm_path -> real_path.
    # Used for score boosting (improvement 2) and auto-caching (improvement 3).
    dirty_paths: dict[str, str] = {}
    if git_root:
        try:
            result = subprocess.run(
                ["git", "-C", str(git_root), "status", "--porcelain", "-u"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                if len(line) >= 4:
                    fname = line[3:].strip().split(" -> ")[-1]
                    real = str((git_root / fname).resolve())
                    norm = real.replace("\\", "/").lower()
                    dirty_paths[norm] = real
        except Exception:
            pass

    # ── Part 1: Project Overview ────────────��───────────────────────────────
    # Always generated (from cache if possible); gives Claude instant structural
    # context without any Glob/Read calls. Budget: up to 1/3 of SESSION_MAX_CHARS,
    # leaving 2/3 for hot codemaps (more useful for large repos).
    overview_text = ""
    overview_budget = SESSION_MAX_CHARS // 3
    try:
        raw_overview = get_or_build_overview(scope_root, git_root or scope_root)
        if len(raw_overview) > overview_budget:
            raw_overview = raw_overview[:overview_budget] + "\n... (truncated)"
        overview_text = raw_overview
    except Exception:
        pass

    index = load_index()

    # Lightweight background cleanup — runs at most once per 24h.
    if index and _should_run_cleanup():
        if run_index_cleanup(index):
            save_index(index)
        _mark_cleanup_done()

    # Auto-cache dirty files not yet in the index (new/unread files being edited).
    # Capped at 10 files to keep session start fast.
    if dirty_paths:
        newly_cached = 0
        for norm, real_path in list(dirty_paths.items())[:10]:
            try:
                if not norm.startswith(project_prefix):
                    continue
                if real_path in index:
                    continue  # already cached
                p = Path(real_path)
                if p.suffix not in CODE_EXTENSIONS or not p.is_file():
                    continue
                stat = p.stat()
                if stat.st_size < MIN_CACHE_LINES * 30:  # rough lines estimate
                    continue
                codemap = generate_codemap(real_path)
                if codemap is None or (codemap.get("lines") or 0) < MIN_CACHE_LINES:
                    continue
                file_hash = sha256_of_file(real_path)
                save_codemap(real_path, codemap, file_hash, stat)
                newly_cached += 1
            except Exception:
                continue
        if newly_cached:
            index = load_index()  # refresh: save_codemap writes its own copy

    # ── Part 2: Hot codemaps ────────────────────────────────────────────────
    # Budget = remaining space after the overview.
    codemaps_budget = SESSION_MAX_CHARS - len(overview_text) - 200  # 200 chars headroom
    codemap_parts: list[str] = []
    served = 0

    if index and codemaps_budget > 500:
        now = time.time()
        DAY = 86400

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for path, entry in index.items():
            norm = path.replace("\\", "/").lower()
            if not norm.startswith(project_prefix):
                continue
            if not os.path.isfile(path):
                continue
            count = entry.get("access_count", 0)
            last = entry.get("last_accessed", 0.0)
            if not last:
                # Never intercepted yet; fall back to generated_at so recently
                # refreshed/cached files still appear in session injection.
                gen_str = entry.get("generated_at", "")
                if gen_str:
                    try:
                        last = time.mktime(time.strptime(gen_str, "%Y-%m-%dT%H:%M:%SZ"))
                    except Exception:
                        last = 0.0
            age = now - last if last else float("inf")
            if age < DAY:
                recency = 10
            elif age < 7 * DAY:
                recency = 5
            elif age < 30 * DAY:
                recency = 2
            else:
                recency = 0
            if norm in dirty_paths:
                recency = max(recency, 15)  # git-modified files outrank everything
            lines_count = entry.get("lines") or MIN_INTERCEPT_LINES
            size_factor = min(lines_count / MIN_INTERCEPT_LINES, 4.0)
            score = (count + recency) * size_factor
            if score > 0:
                scored.append((score, path, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        used_chars = 0

        for _score, path, entry in scored:
            if served >= SESSION_MAX_FILES:
                break
            if not is_cache_valid(path, entry):
                continue
            cm = load_codemap(entry)
            if cm is None:
                continue
            try:
                rel = os.path.relpath(path, cwd)
            except ValueError:
                rel = path
            lines = cm.get("lines", "?")
            count = entry.get("access_count", 0)
            compact_json = json.dumps(cm, separators=(",", ":"), ensure_ascii=False)
            chunk = f"\n## {rel} ({lines}L ×{count})\n{compact_json}\n"
            if used_chars + len(chunk) > codemaps_budget:
                break
            codemap_parts.append(chunk)
            used_chars += len(chunk)
            served += 1

    # ── Assemble final context ──────────────────────────────────────────────
    if not overview_text and served == 0:
        return

    sections: list[str] = []
    if overview_text:
        sections.append(overview_text)
    if codemap_parts:
        sections.append(f"\n# Hot-file CodeMaps: {project_name} ({served} files)")
        sections.extend(codemap_parts)
        sections.append(f"\n_Use Read(offset=N,limit=M) for source lines._")

    context = "\n".join(sections)

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


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
    print(f"  symbols: {symbol_count}  v: {codemap.get('v')}")


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
    elif mode == "session":
        cmd_session()
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
