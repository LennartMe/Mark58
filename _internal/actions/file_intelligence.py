"""Smarter file/folder intelligence for Mark 58.

The action is intentionally analysis-first.  It can explain a file, profile a
folder, find duplicates/large/stale files, and build an organisation plan.  It
only moves files when ``apply`` is explicitly true, making accidental tidy-up
calls harmless by default.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_CATS = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"},
    "Archives": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2"},
    "Code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".html", ".css", ".json", ".yaml", ".yml", ".toml"},
    "Installers": {".exe", ".msi", ".msix", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"},
}
_TEXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".html", ".css", ".log"}


def _resolve(raw: str) -> Path:
    p = Path(str(raw or "")).expanduser()
    if not p.is_absolute():
        aliases = {"desktop": Path.home()/"Desktop", "downloads": Path.home()/"Downloads", "documents": Path.home()/"Documents"}
        p = aliases.get(str(raw).strip().casefold(), p)
    return p.resolve() if p.exists() else p


def _human(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or u == "TB": return f"{x:.1f} {u}"
        x /= 1024
    return f"{x:.1f} TB"


def _category(p: Path) -> str:
    ext = p.suffix.casefold()
    for cat, exts in _CATS.items():
        if ext in exts: return cat
    return "Other"


def _files(root: Path, recursive=True, cap=20000):
    if root.is_file(): return [root]
    out = []
    it = root.rglob("*") if recursive else root.iterdir()
    for p in it:
        try:
            if p.is_file(): out.append(p)
        except Exception: pass
        if len(out) >= cap: break
    return out


def _explain(p: Path) -> str:
    if not p.exists(): return f"File not found: {p}"
    if p.is_dir(): return _analyze_folder(p, recursive=False)
    st = p.stat(); mime = mimetypes.guess_type(str(p))[0] or "unknown"
    lines = [
        f"File: {p.name}", f"Path: {p}", f"Type: {mime} ({p.suffix or 'no extension'})",
        f"Size: {_human(st.st_size)}", f"Modified: {datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}",
        f"Category: {_category(p)}",
    ]
    if p.suffix.casefold() in _TEXT and st.st_size <= 2_000_000:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            lines.append(f"Text: {len(text):,} characters, {text.count(chr(10))+1:,} lines")
            preview = " ".join(text[:1800].split())
            if preview: lines.append("Preview: " + preview[:900])
        except Exception: pass
    return "\n".join(lines)


def _analyze_folder(root: Path, recursive=True) -> str:
    if not root.exists() or not root.is_dir(): return f"Folder not found: {root}"
    fs = _files(root, recursive)
    total = 0; cats = Counter(); exts = Counter(); rows = []
    for p in fs:
        try:
            st = p.stat(); total += st.st_size; cats[_category(p)] += 1; exts[p.suffix.casefold() or "(none)"] += 1; rows.append((st.st_size, p))
        except Exception: pass
    rows.sort(reverse=True, key=lambda x: x[0])
    lines = [f"Folder intelligence: {root}", f"Files scanned: {len(fs):,}", f"Total size: {_human(total)}"]
    if cats: lines.append("Categories: " + ", ".join(f"{k} {v}" for k,v in cats.most_common()))
    if exts: lines.append("Common types: " + ", ".join(f"{k} {v}" for k,v in exts.most_common(8)))
    if rows:
        lines.append("Largest files:")
        lines += [f"- {_human(n)} — {p}" for n,p in rows[:10]]
    return "\n".join(lines)


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024*1024), b""): h.update(b)
    return h.hexdigest()


def _duplicates(root: Path) -> str:
    fs = _files(root, True, 15000); sizes = defaultdict(list)
    for p in fs:
        try:
            n = p.stat().st_size
            if n > 0: sizes[n].append(p)
        except Exception: pass
    groups = []
    for n, same in sizes.items():
        if len(same) < 2: continue
        by_hash = defaultdict(list)
        for p in same:
            try: by_hash[_hash(p)].append(p)
            except Exception: pass
        for hh, members in by_hash.items():
            if len(members) > 1: groups.append((n, hh, members))
    groups.sort(reverse=True, key=lambda x: x[0]*(len(x[2])-1))
    if not groups: return f"No exact duplicate files found under {root}."
    reclaim = sum(n*(len(m)-1) for n,_,m in groups)
    lines = [f"Exact duplicate groups: {len(groups)}", f"Potential reclaimable space: {_human(reclaim)}"]
    for i,(n,_,members) in enumerate(groups[:30],1):
        lines.append(f"{i}. {_human(n)} each")
        lines += [f"   - {p}" for p in members]
    return "\n".join(lines)


def _filter_by(root: Path, kind: str, threshold: float) -> str:
    fs = _files(root, True, 20000); rows=[]; now=time.time()
    for p in fs:
        try:
            st=p.stat()
            value = st.st_size/(1024**2) if kind=="large" else (now-st.st_mtime)/86400
            if value >= threshold: rows.append((value,p,st.st_size,st.st_mtime))
        except Exception: pass
    rows.sort(reverse=True, key=lambda x:x[0])
    label = f">= {threshold:g} MB" if kind=="large" else f"not modified for >= {threshold:g} days"
    lines=[f"Files {label}: {len(rows)}"]
    for value,p,size,mtime in rows[:80]:
        lines.append(f"- {_human(size)} · {datetime.fromtimestamp(mtime).date()} · {p}")
    return "\n".join(lines)


def _organize(root: Path, apply: bool) -> str:
    if not root.exists() or not root.is_dir(): return f"Folder not found: {root}"
    files=[p for p in root.iterdir() if p.is_file()]
    plan=[]
    for p in files:
        cat=_category(p)
        if cat=="Other": continue
        dest=root/cat/p.name
        plan.append((p,dest))
    if not plan: return "Nothing in this folder matches the safe organisation categories."
    if not apply:
        lines=[f"Organisation preview for {root} ({len(plan)} moves). Nothing has been changed."]
        lines += [f"- {src.name} → {dst.parent.name}/" for src,dst in plan[:100]]
        lines.append("Run again with apply=true only if the user explicitly asked to perform these moves.")
        return "\n".join(lines)
    moved=[]; errors=[]
    for src,dst in plan:
        try:
            dst.parent.mkdir(exist_ok=True)
            final=dst; i=2
            while final.exists():
                final=dst.with_name(f"{dst.stem} ({i}){dst.suffix}"); i+=1
            shutil.move(str(src),str(final)); moved.append((src,final))
        except Exception as e: errors.append(f"{src.name}: {e}")
    manifest=root/".mark57_organize_manifest.json"
    try:
        manifest.write_text(json.dumps([{"from":str(a),"to":str(b)} for a,b in moved],indent=2),encoding="utf-8")
    except Exception: pass
    return f"Organised {len(moved)} files into category folders." + (f" {len(errors)} failed: {'; '.join(errors[:5])}" if errors else "")


def run(parameters: dict, **ctx) -> str:
    p=parameters or {}; action=str(p.get("action") or "analyze").strip().lower(); raw=str(p.get("path") or "").strip()
    if not raw: return "Provide a file or folder path."
    path=_resolve(raw)
    if action in {"explain","inspect_file"}: return _explain(path)
    if action in {"analyze","folder","profile"}: return _analyze_folder(path, bool(p.get("recursive", True)))
    if action in {"duplicates","duplicate"}: return _duplicates(path)
    if action=="large": return _filter_by(path,"large",float(p.get("threshold") or 250))
    if action in {"stale","old"}: return _filter_by(path,"stale",float(p.get("threshold") or 90))
    if action in {"organize","sort"}: return _organize(path,bool(p.get("apply",False)))
    return "Unknown action. Use explain, analyze, duplicates, large, stale, or organize."


TOOL={
    "name":"file_intelligence",
    "description":(
        "Understands files and folders: explain a file, profile a folder, find exact duplicates, large/stale files, or organise a messy folder into safe category subfolders. "
        "Organisation is a dry-run unless apply=true. Only set apply=true when the user explicitly asked JARVIS to move/organise the files."
    ),
    "tier":"core",
    "brief":"Analyse, explain, de-duplicate and safely organise files/folders.",
    "parameters":{
        "type":"OBJECT",
        "properties":{
            "action":{"type":"STRING","description":"explain | analyze | duplicates | large | stale | organize"},
            "path":{"type":"STRING","description":"File/folder path, or desktop/downloads/documents."},
            "recursive":{"type":"BOOLEAN","description":"Recurse into subfolders for analysis."},
            "threshold":{"type":"NUMBER","description":"MB for large, days for stale."},
            "apply":{"type":"BOOLEAN","description":"For organize only: actually move files. Defaults false."},
        },
        "required":["action","path"],
    },
    "handler":run,
}
