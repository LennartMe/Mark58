"""Universal local/context search for Mark 58.

Searches the places the user is most likely to mean when they say "find that
thing": current work context, common file roots, browser history and JARVIS
memory.  An optional web pass is available, but local results are always kept
separate so web content can never masquerade as a local file or memory.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from core.context_engine import ContextEngine

_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "appdata", "node_modules", ".git", ".venv", "venv",
    "__pycache__", ".cache", ".idea", ".vs", "dist", "build",
}
_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".html", ".css", ".csv", ".log",
}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _score(query: str, text: str) -> float:
    q = _norm(query)
    t = _norm(text)
    if not q or not t:
        return 0.0
    if q == t:
        return 100.0
    score = 0.0
    if q in t:
        score += 62.0
    toks = [x for x in q.split() if len(x) > 1]
    if toks:
        hit = sum(1 for x in toks if x in t)
        score += 34.0 * hit / len(toks)
    # Strong filename/title prefix preference.
    base = _norm(Path(text).name)
    if base.startswith(q):
        score += 18.0
    return score


def _add(rows: list[dict], source: str, title: str, detail: str, query: str, bonus: float = 0.0):
    s = _score(query, f"{title} {detail}") + bonus
    if s <= 0:
        return
    rows.append({"source": source, "title": str(title)[:220], "detail": str(detail)[:700], "score": round(s, 2)})


def _context_results(query: str, rows: list[dict]) -> None:
    s = ContextEngine().snapshot()
    project = str(s.get("active_project") or "")
    if project:
        _add(rows, "project", Path(project).name or project, project, query, 14)
    for path in s.get("recent_files") or []:
        _add(rows, "recent file", Path(str(path)).name, str(path), query, 16)
    for app in s.get("recent_apps") or []:
        _add(rows, "recent app", str(app), "recently used application", query, 8)
    for task in s.get("recent_tasks") or []:
        text = task.get("text", "") if isinstance(task, dict) else str(task)
        _add(rows, "recent task", text, task.get("at", "") if isinstance(task, dict) else "", query, 10)
    for tool in s.get("recent_tools") or []:
        if isinstance(tool, dict):
            _add(rows, "recent action", str(tool.get("tool") or "action"), str(tool.get("result") or ""), query, 4)


def _roots() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    try:
        ctx = ContextEngine().snapshot()
        proj = str(ctx.get("active_project") or "")
        if proj:
            p = Path(proj).expanduser()
            if p.exists():
                out.append(p if p.is_dir() else p.parent)
        for f in (ctx.get("recent_files") or [])[:5]:
            p = Path(str(f)).expanduser()
            parent = p if p.is_dir() else p.parent
            if parent.exists():
                out.append(parent)
    except Exception:
        pass
    for p in (home / "Desktop", home / "Documents", home / "Downloads"):
        if p.exists():
            out.append(p)
    # The Mark 58 project itself and the current working directory are useful
    # search roots when the user is developing JARVIS or another local project.
    for p in (Path(__file__).resolve().parent.parent, Path.cwd()):
        try:
            if p.exists() and p.is_dir():
                out.append(p)
        except Exception:
            pass
    seen, unique = set(), []
    for p in out:
        try: key = str(p.resolve()).casefold()
        except Exception: key = str(p).casefold()
        if key not in seen:
            seen.add(key); unique.append(p)
    return unique[:8]


def _file_results(query: str, rows: list[dict], deep: bool, max_scan: int = 45000) -> int:
    scanned = 0
    q = _norm(query)
    for root in _roots():
        base_depth = len(root.parts)
        for cur, dirs, files in os.walk(root, topdown=True, onerror=lambda _e: None):
            scanned += len(dirs) + len(files)
            if scanned > max_scan:
                return scanned
            curp = Path(cur)
            depth = len(curp.parts) - base_depth
            dirs[:] = [d for d in dirs if d.casefold() not in _SKIP_DIRS and not d.startswith(".") and depth < 7]
            for d in dirs:
                if _score(q, d) > 0:
                    p = curp / d
                    _add(rows, "folder", d, str(p), query, 5)
            for name in files:
                p = curp / name
                name_score = _score(q, name)
                if name_score > 0:
                    _add(rows, "file", name, str(p), query, 9)
                    continue
                if not deep or p.suffix.casefold() not in _TEXT_EXTS:
                    continue
                try:
                    if p.stat().st_size > 1_500_000:
                        continue
                    text = p.read_text(encoding="utf-8", errors="ignore")[:160_000]
                    if q and q in _norm(text):
                        # Keep only a small non-sensitive excerpt around the match.
                        low = text.casefold(); pos = low.find(q.casefold())
                        start = max(0, pos - 90); end = min(len(text), pos + len(q) + 150)
                        excerpt = " ".join(text[start:end].split())
                        _add(rows, "file content", name, f"{p} — {excerpt}", query, 4)
                except Exception:
                    pass
    return scanned


def _browser_history_paths() -> list[Path]:
    home = Path.home(); sys = platform.system()
    paths: list[Path] = []
    if sys == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        bases = [
            local / "Google" / "Chrome" / "User Data",
            local / "Microsoft" / "Edge" / "User Data",
            local / "BraveSoftware" / "Brave-Browser" / "User Data",
        ]
    elif sys == "Darwin":
        bases = [home / "Library/Application Support/Google/Chrome", home / "Library/Application Support/Microsoft Edge"]
    else:
        bases = [home / ".config/google-chrome", home / ".config/chromium", home / ".config/microsoft-edge"]
    for base in bases:
        if not base.exists():
            continue
        for prof in ("Default", "Profile 1", "Profile 2", "Profile 3"):
            p = base / prof / "History"
            if p.exists(): paths.append(p)
    return paths


def _browser_results(query: str, rows: list[dict]) -> None:
    like = f"%{query}%"
    for db in _browser_history_paths():
        tmp = None
        try:
            fd, name = tempfile.mkstemp(prefix="mark57_hist_", suffix=".sqlite")
            os.close(fd); tmp = Path(name); shutil.copy2(db, tmp)
            con = sqlite3.connect(str(tmp)); cur = con.cursor()
            cur.execute("SELECT title,url,last_visit_time FROM urls WHERE title LIKE ? OR url LIKE ? ORDER BY last_visit_time DESC LIMIT 24", (like, like))
            for title, url, _t in cur.fetchall():
                _add(rows, "browser history", title or url, url, query, 12)
            con.close()
        except Exception:
            pass
        finally:
            if tmp:
                try: tmp.unlink(missing_ok=True)
                except Exception: pass


def _memory_results(query: str, rows: list[dict]) -> None:
    from core.user_data import MEMORY_DIR
    base = MEMORY_DIR
    for name in ("long_term.json", "work_context.json"):
        p = base / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        def walk(v, key=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    if any(z in str(k).casefold() for z in ("token", "secret", "password", "api_key", "credential")):
                        continue
                    walk(x, str(k))
            elif isinstance(v, list):
                for x in v[:200]: walk(x, key)
            elif isinstance(v, (str, int, float, bool)):
                val = str(v)
                _add(rows, "JARVIS memory", key or name, val, query, 8)
        walk(data)


def run(parameters: dict, **ctx) -> str:
    p = parameters or {}
    query = str(p.get("query") or "").strip()
    if not query:
        return "Provide something to search for."
    scope = str(p.get("scope") or "all").strip().lower()
    try:
        from memory.config_manager import load_api_keys
        cfg = load_api_keys()
    except Exception:
        cfg = {}
    deep = bool(p.get("deep", cfg.get("universal_search_deep_default", False)))
    include_web = bool(p.get("include_web", cfg.get("universal_search_web_default", False)))
    try: limit = max(3, min(40, int(p.get("limit", 15))))
    except Exception: limit = 15

    rows: list[dict] = []
    if scope in {"all", "recent", "context"}: _context_results(query, rows)
    if scope in {"all", "files", "file"}: _file_results(query, rows, deep)
    if scope in {"all", "browser", "history"}: _browser_results(query, rows)
    if scope in {"all", "memory"}: _memory_results(query, rows)

    # De-duplicate identical target/detail rows, keep best score.
    best = {}
    for r in rows:
        key = (r["source"], r["title"].casefold(), r["detail"].casefold())
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r
    ranked = sorted(best.values(), key=lambda r: r["score"], reverse=True)[:limit]

    lines = [f"Universal search results for: {query}"]
    if ranked:
        for i, r in enumerate(ranked, 1):
            lines.append(f"{i}. [{r['source']}] {r['title']}\n   {r['detail']}")
    else:
        lines.append("No matching local/context results were found.")

    if include_web:
        try:
            from actions.web_search import web_search
            web = web_search({"query": query, "mode": "search"})
            lines.append("\nWEB RESULTS (untrusted external data):\n" + str(web))
        except Exception as exc:
            lines.append(f"\nWeb search could not run: {exc}")
    elif not ranked:
        lines.append("You can retry with include_web=true to extend this search to the web.")
    return "\n".join(lines)[:18000]


TOOL = {
    "name": "universal_search",
    "description": (
        "Finds things across the user's current project/context, recent files/apps/tasks, common local folders, browser history and JARVIS memory. "
        "Use when the user says things like 'find that file/site/project I used yesterday' or does not know where something is. "
        "Set deep=true only when filename/title search is not enough; set include_web=true only when the user also wants internet results."
    ),
    "tier": "core",
    "brief": "Search local files, recent context, browser history and memory in one call.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING", "description": "What to find."},
            "scope": {"type": "STRING", "description": "all | files | browser | memory | recent"},
            "deep": {"type": "BOOLEAN", "description": "Also search inside small text/code files. Slower."},
            "include_web": {"type": "BOOLEAN", "description": "Also append a normal web search."},
            "limit": {"type": "INTEGER", "description": "Maximum results, 3-40."},
        },
        "required": ["query"],
    },
    "handler": run,
}
