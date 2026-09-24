#!/usr/bin/env python3
"""history.py — browse the backup history (db.json) in the terminal.

Curses-based view with three levels:
  * Sessions (newest first, grouped by generation): date, type, file count,
    size before→after, saved, and compression percentage.
  * Enter/→ opens a session and lists its ARCHIVES (root + subfolders) —
    overview only, no files.
  * Enter/→ on an archive lists its individual FILES.
  * ↑/↓ navigates, PgUp/PgDn/Home/End jumps, ←/Esc goes back (never quits),
    Q quits the whole app.

Standalone and dependency-free — requires neither PyYAML nor zstandard,
just the Python standard library (curses is available on Linux/macOS).
"""

from __future__ import annotations

import curses
import datetime as dt
import json
import locale
import os


DEFAULT_DB = "db.json"

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


# --------------------------------------------------------------------------- #
# Formatting / loading
# --------------------------------------------------------------------------- #
def human(nbytes) -> str:
    """Bytes -> human-readable string (B, KB, MB, GB ...). Handles negative values."""
    nbytes = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(nbytes) < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(nbytes)} {unit}"
            return f"{nbytes:.2f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.2f} PB"


def load_db(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read().strip()
        if not data:
            return {"sessions": []}
        db = json.loads(data)
        if not isinstance(db, dict):
            return {"sessions": []}
        db.setdefault("sessions", [])
        return db
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sessions": []}


def fmt_date(iso_str) -> str:
    if not iso_str:
        return "—"
    try:
        d = dt.datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return str(iso_str)[:16]
    return f"{d.day:2d} {_MONTHS[d.month - 1]} {d.strftime('%H:%M')}"


def session_row(session: dict) -> str:
    """A row in the overview list (without the marker prefix)."""
    t = session.get("totals", {})
    kind = session.get("session_type", "full")
    date = fmt_date(session.get("started_at"))
    if t.get("archive_count", 0) == 0:
        return f"{kind:<11} {date:>13}   nothing packed"
    files = t.get("file_count", 0)
    before = human(t.get("size_before", 0))
    after = human(t.get("size_after", 0))
    pct = t.get("compression_percent", 0.0)
    saved = human(t.get("saved_bytes", 0))
    return (f"{kind:<11} {date:>13}  {files:>3} files  "
            f"{before:>9} → {after:>9}  {pct:6.1f}%  saved {saved}")


def collect_archives(session: dict) -> list:
    """The archives in a session (root + subfolders), as selectable entries.
    No files here — overview per archive only."""
    backups = session.get("backups", [])
    multi = len(backups) > 1
    out = []
    for b in backups:
        src = b.get("source_path", "")
        prefix = (os.path.basename(src.rstrip("/")) + "/") if multi else ""
        root = b.get("root")
        if root:
            out.append({"label": prefix + (root.get("archive_name") or "root"),
                        "arc": root})
        for sub in b.get("subfolders", []):
            name = sub.get("archive_name") or sub.get("subfolder_name", "?")
            out.append({"label": prefix + name, "arc": sub})
    return out


def archive_row(entry: dict) -> str:
    """An archive row (root or subfolder) in the session's archive list."""
    arc = entry["arc"]
    name = entry["label"]
    if arc.get("file_count", 0) == 0:
        return f"▸ {name:<30} (empty)"
    return (f"▸ {name:<30} "
            f"{human(arc.get('size_before', 0)):>9} → "
            f"{human(arc.get('size_after', 0)):>9}  "
            f"{arc.get('compression_percent', 0.0):6.1f}%  "
            f"{arc.get('file_count', 0):>3} files")


def file_row(f: dict) -> str:
    """A file row in an archive."""
    return (f"· {f.get('arcname', '?')}   "
            f"{human(f.get('size', 0))}   "
            f"changed {fmt_date(f.get('mtime_iso'))}")


def session_header(session: dict) -> list:
    """Context lines at the top of the archive list for a session."""
    t = session.get("totals", {})
    return [
        f"Session {session.get('session_id', '?')}   "
        f"({session.get('session_type', 'full')})",
        f"{fmt_date(session.get('started_at'))}    "
        f"{t.get('archive_count', 0)} archives, {t.get('file_count', 0)} files    "
        f"{human(t.get('size_before', 0))} → {human(t.get('size_after', 0))}"
        f"    saved {human(t.get('saved_bytes', 0))} "
        f"({t.get('compression_percent', 0.0)} %)",
    ]


def archive_header(entry: dict) -> list:
    """Context lines at the top of the file list for an archive."""
    arc = entry["arc"]
    return [
        entry["label"],
        f"{human(arc.get('size_before', 0))} → {human(arc.get('size_after', 0))}"
        f"    {arc.get('compression_percent', 0.0)} %"
        f"    {arc.get('file_count', 0)} files"
        f"    {arc.get('duration_seconds', 0)} s",
    ]


# --------------------------------------------------------------------------- #
# Grouping: one full + the incrementals that follow it (kept separate)
# --------------------------------------------------------------------------- #
def build_generations(sessions_chrono: list) -> list:
    """Split the sessions (chronologically) into generations. A new
    generation starts at every full backup; incrementals belong to the
    nearest preceding full backup. No statistics are merged — each session
    is kept as is."""
    gens: list = []
    for s in sessions_chrono:
        if s.get("session_type") == "full" or not gens:
            gens.append([s])
        else:
            gens[-1].append(s)
    return gens


def build_rows(sessions_chrono: list) -> list:
    """Flat list of rows, grouped by generation. Newest generation first;
    within a generation chronologically (full on top, then its
    incrementals). Each row points to a standalone session with its own
    statistics."""
    rows = []
    for gen in reversed(build_generations(sessions_chrono)):
        for idx, s in enumerate(gen):
            rows.append({
                "session": s,
                "role": "root" if idx == 0 else "child",
                "last": idx == len(gen) - 1,
            })
    return rows


def row_line(entry: dict) -> str:
    """Tree marker + the session row."""
    base = session_row(entry["session"])
    if entry["role"] == "root":
        return "● " + base
    return "  " + ("└ " if entry["last"] else "├ ") + base


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def _safe(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that never crashes at the edge of the screen."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
    except curses.error:
        pass


HELP_LIST = " ↑/↓ select   Enter open session   ←/Esc menu   Q quit "
HELP_ARCHIVES = " ↑/↓ select   Enter show files   ←/Esc back   Q quit "
HELP_FILES = " ↑/↓ scroll   ←/Esc back   Q quit "

EMPTY_LIST = "No runs in db.json yet."
EMPTY_ITEMS = "(nothing to show)"


def make_frame(kind, items, render, header, title, help_line, empty) -> dict:
    return {"kind": kind, "items": items, "render": render, "header": header,
            "title": title, "help": help_line, "empty": empty,
            "sel": 0, "top": 0}


def open_child(frame: dict):
    """Build the next level's frame from the selected entry, or None if deepest."""
    if not frame["items"]:
        return None
    if frame["kind"] == "list":
        s = frame["items"][frame["sel"]]["session"]
        return make_frame("archives", collect_archives(s), archive_row,
                          session_header(s), "ARCHIVES IN SESSION",
                          HELP_ARCHIVES, "(no archives were created in this run)")
    if frame["kind"] == "archives":
        entry = frame["items"][frame["sel"]]
        return make_frame("files", entry["arc"].get("files", []), file_row,
                          archive_header(entry), "FILES IN ARCHIVE",
                          HELP_FILES, "(no files)")
    return None  # "files" is the deepest level


def _frame_layout(stdscr, frame):
    """(start_row, area) — where the list starts and how many rows fit."""
    h, _ = stdscr.getmaxyx()
    hdr = frame["header"]
    start = 1 + (len(hdr) + 1 if hdr else 0)  # title + optional header + blank line
    area = max(1, h - 1 - start)              # minus footer
    return start, area


def _draw_frame(stdscr, frame, start: int, area: int) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    _safe(stdscr, 0, 0, ("  " + frame["title"]).ljust(w), curses.A_BOLD)
    for i, line in enumerate(frame["header"]):
        _safe(stdscr, 1 + i, 2, line, curses.A_DIM)
    _safe(stdscr, h - 1, 0, frame["help"].ljust(w), curses.A_REVERSE)

    items = frame["items"]
    if not items:
        _safe(stdscr, start, 2, frame["empty"])
        stdscr.refresh()
        return

    sel, top = frame["sel"], frame["top"]
    row = start
    for i in range(top, min(top + area, len(items))):
        attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
        _safe(stdscr, row, 0, (" " + frame["render"](items[i])).ljust(w), attr)
        row += 1
    if len(items) > area:
        _safe(stdscr, 0, w - 14, f"{sel + 1}/{len(items)}", curses.A_BOLD)
    stdscr.refresh()


# --------------------------------------------------------------------------- #
# Main loop — levels: sessions -> archives -> files.
#   Enter/→ opens the next level, ←/Esc/Backspace goes back (never quits),
#   Q quits the whole app from any level.
# --------------------------------------------------------------------------- #
def _run(stdscr, db_path: str) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    try:
        curses.set_escdelay(25)   # makes Esc responsive (otherwise ~1s delay)
    except (AttributeError, curses.error):
        pass

    db = load_db(db_path)
    stack = [make_frame("list", build_rows(db.get("sessions", [])), row_line,
                        [], "BACKUP HISTORY", HELP_LIST, EMPTY_LIST)]

    while True:
        frame = stack[-1]
        start, area = _frame_layout(stdscr, frame)
        n = len(frame["items"])
        if n:
            frame["sel"] = max(0, min(frame["sel"], n - 1))
            if frame["sel"] < frame["top"]:
                frame["top"] = frame["sel"]
            elif frame["sel"] >= frame["top"] + area:
                frame["top"] = frame["sel"] - area + 1

        _draw_frame(stdscr, frame, start, area)
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            return "quit"          # leave the whole application
        if key in (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
            if len(stack) > 1:
                stack.pop()        # go back one step in the view
            else:
                return "menu"      # at the top level: back out to the main menu
            continue
        if not n:
            continue
        if key in (curses.KEY_UP, ord("k")):
            frame["sel"] -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            frame["sel"] += 1
        elif key == curses.KEY_NPAGE:
            frame["sel"] = min(n - 1, frame["sel"] + area)
        elif key == curses.KEY_PPAGE:
            frame["sel"] = max(0, frame["sel"] - area)
        elif key == curses.KEY_HOME:
            frame["sel"] = 0
        elif key == curses.KEY_END:
            frame["sel"] = n - 1
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
            child = open_child(frame)
            if child is not None:
                stack.append(child)


def browse(db_path: str = DEFAULT_DB) -> str:
    """Open the interactive history view for db.json at 'db_path'.

    Returns 'menu' if the user backed out (Esc/← at the top level) or
    'quit' if the user chose to quit the app (Q)."""
    locale.setlocale(locale.LC_ALL, "")  # UTF-8 in curses (arrows, box characters)
    return curses.wrapper(_run, db_path) or "menu"


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Browse backup history (db.json)")
    ap.add_argument("-d", "--db", default=DEFAULT_DB)
    args = ap.parse_args()
    browse(args.db)


if __name__ == "__main__":
    main()
