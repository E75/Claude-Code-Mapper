# Claude-Code-Mapper

Cross-session code-map cache for Claude Code. Reads a file **once**, stores a compact
structural map on disk, and serves that map to Claude on every subsequent read until the
file changes. Saves tokens on repeated reads across sessions.

## The problem

Claude Code has no cross-session memory of file content. Each new session re-reads files
from scratch, paying full token cost every time. Within a session, prompt caching helps
for ~5 minutes. Across sessions — nothing.

## The approach

Three hooks on the `Read` tool:

1. **PreToolUse**: if a cached map exists **and** the file hash is unchanged,
   the hook blocks the real Read and hands Claude the cached map instead.
2. **PostToolUse**: after a real Read succeeds, the file is parsed and the
   resulting code map is written to disk.
3. The cache lives on disk, so maps survive restarts and are available the next day.

## Change detection

Per file, the cache stores `(hash, size, mtime)`:

- **Fast path**: `size + mtime` match cache → serve it. Runs in under a millisecond.
- **Slow path**: `size` or `mtime` differs → compute SHA-256 of the content.
  - Hash matches cache (e.g. `git checkout` touched the mtime but content is identical)
    → serve the map, refresh the quick-check fields.
  - Hash differs → cache is stale → let the real Read proceed, PostToolUse rebuilds the map.

SHA-256 is the ground truth; `size`/`mtime` are only used as a cheap pre-filter.

## Parser pipeline

Tried in order, first success wins:

1. **XAML parser** — built-in, for `.xaml`/`.axaml` (ctags doesn't parse these).
   Captures `x:Class`, `x:Name`, `x:Key`, event handlers (`Clicked`, `Tapped`, …),
   root element, and declared namespaces. Useful for .NET MAUI / WPF / Avalonia.
2. **`universal-ctags`** (preferred for everything else) — JSON output, 40+ languages.
3. **Regex fallback** — built-in lightweight sniffer for TS/JS/TSX (incl. class methods
   and decorators), C# (methods, ctors, **records**, **properties**, events, delegates,
   file-scoped namespaces), Python, Go, Java, Rust, Godot, and similar.
4. **Preview fallback** — if nothing matches, the first 80 lines are stored.

### Semantic enrichment (all sources)

Every map is enriched with context that's already in the code (zero LLM cost):

- **`header_doc`** — top-of-file comment/docstring block.
- **`imports`** — first 30 `import`/`using`/`use` targets.
- **`todos`** — `TODO`/`FIXME`/`HACK`/`XXX`/`NOTE` markers with line numbers.
- **Per symbol `doc`** — the JSDoc / XML `<summary>` / Python docstring / `///` block
  directly above (or inside, for Python) the symbol.

This means Claude sees not just *where* `getUser` lives but also its one-line purpose,
the file's reason for existing, what it imports, and any pending FIXMEs — without
loading the function bodies.

Install ctags on Windows:

```
winget install UniversalCtags.Ctags
# or
choco install universal-ctags
```

The fallback works without ctags; installing it just gives you more accurate maps.

## Installation

### 1. Clone or copy

```bash
git clone https://github.com/<you>/Claude-Code-Mapper ~/dev/Claude-Code-Mapper
```

Or keep it where you have it (e.g. `C:/Users/xxx/Documents/GitHub/Claude-Code-Mapper`).

### 2. Wire the hooks into Claude Code

Add the following to `~/.claude/settings.json` (or merge with your existing `hooks` block):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "python C:/Users/xxx/Documents/GitHub/Claude-Code-Mapper/codemap_hook.py pre"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "python C:/Users/xxx/Documents/GitHub/Claude-Code-Mapper/codemap_hook.py post"
          }
        ]
      }
    ]
  }
}
```

See `settings.example.json` for a standalone copy.

### 3. Verify

```bash
python codemap_hook.py status
```

Should print something like:

```
Cache directory: C:\Users\xxx\.claude\codemap-cache
ctags available: False
Cached files: 0
```

## CLI

| Command | What it does |
|---|---|
| `python codemap_hook.py status` | Show cache directory, ctags availability, list of cached files. |
| `python codemap_hook.py show <path>` | Print the cached map for a file. |
| `python codemap_hook.py refresh <path>` | Force-regenerate the map for a file. |
| `python codemap_hook.py clear` | Delete the entire cache. |
| `python codemap_hook.py pre` / `post` | Hook entry points — called by Claude Code, not by hand. |

## Cache layout

```
~/.claude/codemap-cache/
├── index.json              manifest: { abs_path: { hash, size, mtime, map_file, ... } }
└── maps/
    ├── 7a3fb2c1e4d5f601.json    code map for src/auth/login.ts
    ├── 9e8d4c2a1f30b8c2.json    code map for src/users/user.service.ts
    └── ...
```

Each map file (schema v2, token-optimised — grouped by kind, short field aliases):

```json
{
  "v": 2,
  "path": "C:/project/src/auth/login.ts",
  "lang": "typescript",
  "src": "ctags",
  "lines": 82,
  "header_doc": "Login + token refresh helpers.",
  "imports": ["bcrypt", "./http"],
  "todos": [{"l": 45, "k": "TODO", "t": "rotate refresh tokens"}],
  "syms": {
    "interface": [{"n": "LoginCredentials", "l": 10}],
    "class":     [{"n": "AuthError", "l": 20}],
    "function":  [
      {"n": "login",        "l": 27, "s": "(creds: LoginCredentials)",
       "d": "Authenticate and return a session token."},
      {"n": "refreshToken", "l": 45},
      {"n": "hashPassword", "l": 65}
    ]
  }
}
```

**Field legend:** `syms` groups symbols by kind (`kind` becomes the key, so it's not repeated).
Per symbol: `n` = name, `l` = line, `s` = signature, `d` = doc-comment, `e` = end line,
`c` = scope (class/namespace), `y` = type. TODOs use `l`/`k`/`t` for line/kind/text.
The hook injects this as compact (no-indent) JSON, which cuts ~35% of tokens vs the old
indented-long-key format.

## What it caches (and what it skips)

**Cached:** files with a recognised code extension under 10 MB
(`.ts/.tsx/.js/.jsx/.py/.cs/.java/.kt/.go/.rs/.rb/.php/.cpp/.h/.xaml/.axaml/.razor/.cshtml/.vb/...`
— full list in `CODE_EXTENSIONS` inside the script).

File-size note: the 2000-line default of Claude's `Read` tool does **not** apply here.
The mapper reads the whole file directly from disk (up to 10 MB) every time the cache
is built, so a 5000-line file gets a complete symbol map even though Claude itself
would only see the first 2000 lines on a default `Read`.

**Skipped:**

- Partial reads (Claude passing `offset`/`limit`) — Claude is asking for a specific
  slice, the hook stays out of the way.
- Non-code files (JSON data, CSV, logs, markdown, config) — low signal for a code map.
- Files larger than 10 MB — not the target use case.

## Token savings

Rough rule of thumb, re-read of an unchanged file:

| File size   | Raw read | Cached map | Saving |
|-------------|---------:|-----------:|-------:|
| ~200 lines  |  ~1.0k tok |   ~0.3k tok |   ~70% |
| ~500 lines  |  ~2.5k tok |   ~0.5k tok |   ~80% |
| ~1500 lines |  ~7.5k tok |   ~1.0k tok |   ~87% |

The first read of a file still pays full cost (plus small parser overhead) — the
wins compound from the second read onward, especially across sessions.

The map also tells Claude the exact line numbers of every symbol, so when it does
need the body of a specific method it can do `Read(file, offset=27, limit=20)`
instead of loading the whole file.

## Limitations

- **ctags on Windows** needs a one-time install. Without it, the regex fallback is
  used — works for most mainstream languages but misses generics/nested scopes.
- **Non-hook platforms** — this is Claude-Code-specific. Other agents that don't
  fire hooks on tool use won't benefit.
- **Not a semantic index** — this catches *what symbols exist and where*. It doesn't
  capture cross-file relationships, call graphs, or semantic summaries.
- **Claude can still ignore the cache** — if Claude insists on re-reading with no
  offset, PreToolUse serves the map and blocks, but Claude may then ask for a slice.
  That slice is served by real Read (much smaller) and is not cached.
- **Structural, not semantic** — the map shows *what symbols exist* and (via enrichment)
  their doc-comments, but **not** function bodies, control flow, or error-handling
  branches. When Claude needs that depth, the PreToolUse message tells it to:
  ```
  Read(file, offset=<line>, limit=<span>)     # for one symbol
  Read(file, offset=1, limit=<total>)         # bypasses the cache, full file
  ```

## Uninstall

Remove the `hooks` entries from `~/.claude/settings.json` and delete
`~/.claude/codemap-cache/`.
