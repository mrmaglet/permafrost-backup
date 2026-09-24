#!/usr/bin/env python3
"""
backup.py — tar+zstd backup driven by config.yml.

Focus of this version:
  * Reads 'backups' from config.yml and compresses the listed folders/files.
  * Honors 'exclude' per path.
  * Compression: tar is streamed through an optional tool (zstd or xz) as
    an external process, with level and threads per tool. Chosen per
    snapshot (default.compression_tool) or per path (compression_tool).
  * Split: the archive can be split into N GB parts via 'split' – the flow
    is piped as tar | zstd/xz | split so no uncompressed intermediate data
    takes up disk space. Parts are named archive.000, archive.001, ...
    (split: 0 = no split).
  * Recreates the source path under 'output_path'.
        /server/fileserver/Programs  ->  <output_path>/server/fileserver/Programs
  * Does NOT create one big archive. In the target folder it creates:
        root.tar.zstd          -> all files directly in the source folder
        <subfolder>.tar.zstd    -> one archive per subfolder
  * Preview + confirmation before running.
  * Progress bar while running.
  * Each run is saved as its own object (history) in db.json with facts
    per subfolder and for root (size before/after, time, compression
    ratio, amount saved). Per file, only size + mtime are saved, so it
    can later be determined whether the file changed on disk after the
    backup.
  * When overwriting existing archives, asks: Yes / No / all / none.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

try:
    import yaml
except ImportError:
    sys.exit("Missing PyYAML. Install it in your venv: pip install PyYAML")


# --------------------------------------------------------------------------- #
# Compression tools
# --------------------------------------------------------------------------- #
# Two tools are supported, both as external CLI processes so we can pipe
# tar | compression | split in a single flow (saves disk space – no
# temporary uncompressed intermediate data is written). The tool is chosen
# per snapshot via default.compression_tool or per path via 'compression_tool'.
TOOL_ZSTD = "zstd"
TOOL_XZ = "xz"
VALID_TOOLS = (TOOL_ZSTD, TOOL_XZ)
DEFAULT_TOOL = TOOL_ZSTD

# Valid level range per tool. The level is interpreted "according to each
# tool": zstd 1-22 (>19 requires --ultra), xz 0-9. 0/empty => the tool's
# default level.
LEVEL_RANGES = {TOOL_ZSTD: (1, 22), TOOL_XZ: (0, 9)}

# Archive extension per tool (compressed) and uncompressed (tar only).
COMPRESSED_EXT = {TOOL_ZSTD: ".tar.zstd", TOOL_XZ: ".tar.xz"}
ARCHIVE_EXT_PLAIN = ".tar"
# All extensions we recognize as "an archive" (e.g. in existing_archive_dirs).
ARCHIVE_EXTS = tuple(COMPRESSED_EXT.values()) + (ARCHIVE_EXT_PLAIN,)

# Split: numeric suffix 000, 001, ... (three digits = up to 1000 parts).
SPLIT_SUFFIX_LEN = 3
_ARCHIVE_NAME_RE = re.compile(r"\.tar(\.(zstd|xz))?(\.\d{3})?$")

# threads=-1 = all logical CPU cores. Used when the config says 0/empty (i.e.
# "auto"). Can be overridden per snapshot/path via 'threads'. When calling
# xz/zstd, -1 is translated to -T0 (which means "all cores" in both tools).
THREADS_ALL = -1
# Backward-compatible alias.
ZSTD_THREADS_ALL = THREADS_ALL


def clamp_level(tool: str, level):
    """Compression level interpreted according to the tool's range.

    Empty/None/0 => None (the tool's default level). Values outside the
    range are clamped into the valid range instead of being passed raw to
    the tool.
    """
    if not level:
        return None
    try:
        level = int(level)
    except (TypeError, ValueError):
        return None
    lo, hi = LEVEL_RANGES.get(tool, LEVEL_RANGES[DEFAULT_TOOL])
    return max(lo, min(hi, level))


def compressor_cmd(tool: str, level, threads: int) -> list[str]:
    """CLI command to compress stdin -> stdout with the given tool."""
    t = 0 if threads is None or threads < 0 else int(threads)
    lvl = clamp_level(tool, level)
    if tool == TOOL_XZ:
        cmd = ["xz", "-z", "-c", "-T", str(t)]
        if lvl is not None:
            cmd.append(f"-{lvl}")
        return cmd
    # zstd
    cmd = ["zstd", "-q", "-c", f"-T{t}"]
    if lvl is not None:
        cmd.append(f"-{lvl}")
        if lvl > 19:
            cmd.append("--ultra")
    return cmd


def archive_ext(tool: str, compress: bool) -> str:
    if not compress:
        return ARCHIVE_EXT_PLAIN
    return COMPRESSED_EXT.get(tool, COMPRESSED_EXT[DEFAULT_TOOL])


def root_archive_name(tool: str, compress: bool) -> str:
    return "root" + archive_ext(tool, compress)


def is_archive_name(name: str) -> bool:
    """True if the filename looks like an archive or a split part of one."""
    return bool(_ARCHIVE_NAME_RE.search(name))


def existing_archive_parts(archive_path: str) -> list[str]:
    """Existing outputs for an archive: single file and/or split parts."""
    out = []
    if os.path.exists(archive_path):
        out.append(archive_path)
    pattern = glob.escape(archive_path) + "." + "[0-9]" * SPLIT_SUFFIX_LEN
    out.extend(sorted(glob.glob(pattern)))
    return out

# Tolerance when comparing mtime (seconds) in incremental mode.
MTIME_EPS = 1e-6


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def human(nbytes: float) -> str:
    """Bytes -> human-readable string (B, KB, MB, GB ...)."""
    nbytes = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(nbytes) < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(nbytes)} {unit}"
            return f"{nbytes:.2f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.2f} PB"


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def format_duration(seconds: float) -> str:
    """Human-readable duration: seconds <60s, minutes <60min, otherwise HH:MM."""
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m} min {s} s"
    h, rem = divmod(total, 3600)
    return f"{h:02d}:{rem // 60:02d}"


def gb_decimal(nbytes: int) -> str:
    """Split size in decimal GB (10^9) + exact byte count, space-grouped thousands."""
    grouped = f"{int(nbytes):,}".replace(",", " ")
    return f"{nbytes / 1_000_000_000:g} GB ({grouped} bytes)"


# --------------------------------------------------------------------------- #
# Progress bar
# --------------------------------------------------------------------------- #
class ProgressBar:
    def __init__(self, total_bytes: int, width: int = 38):
        self.total = max(total_bytes, 1)
        self.done = 0
        self.width = width
        self.label = ""
        self._interactive = sys.stdout.isatty()

    def set_label(self, label: str) -> None:
        self.label = label
        self._render()

    def update(self, nbytes: int) -> None:
        self.done += nbytes
        self._render()

    def _render(self) -> None:
        if not self._interactive:
            return
        frac = min(self.done / self.total, 1.0)
        filled = int(frac * self.width)
        bar = "█" * filled + "░" * (self.width - filled)
        label = (self.label[:28] + "…") if len(self.label) > 29 else self.label
        sys.stdout.write(
            f"\r[{bar}] {frac * 100:5.1f}%  "
            f"{human(self.done)}/{human(self.total)}  {label:<29}"
        )
        sys.stdout.flush()

    def finish(self) -> None:
        if self._interactive:
            sys.stdout.write("\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Overwrite policy (Yes / No / all / skip all)
# --------------------------------------------------------------------------- #
class OverwritePolicy:
    def __init__(self):
        self.sticky: bool | None = None  # True=all, False=skip all, None=ask

    def allow(self, archive_path: str) -> bool:
        if self.sticky is not None:
            return self.sticky
        rel = archive_path
        print(f"\nArchive already exists:\n  {rel}")
        while True:
            ans = input("Overwrite?  [Y]es / [N]o / [A]ll / [S]kip all : ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            if ans in ("a", "all"):
                self.sticky = True
                return True
            if ans in ("s", "skip", "none"):
                self.sticky = False
                return False
            print("Answer with Y, N, A (all) or S (skip all).")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def ensure_config(path: str) -> None:
    """Copy config.example.yml -> path if no config exists yet."""
    if os.path.exists(path):
        return
    example = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.yml")
    if not os.path.exists(example):
        return
    shutil.copyfile(example, path)
    print(f"No {path} found – created one from config.example.yml. "
          f"Edit it before running the backup again.")


def load_config(path: str) -> dict:
    ensure_config(path)
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if "backups" not in cfg or not cfg["backups"]:
        sys.exit("config.yml is missing the 'backups' list.")
    if "config" not in cfg or not cfg["config"].get("output_path"):
        sys.exit("config.yml is missing config.output_path.")
    return cfg


def norm(path: str) -> str:
    return os.path.normpath(path)


def backup_line_numbers(config_path: str) -> list[dict]:
    """Line numbers per backup entry from config.yml (discarded by safe_load).

    Returns a list in the same order as 'backups' with:
        {'path': <line>, 'exclude_lines': [<line>, ...]}
    'exclude_lines' is in the same order as normalize_excludes() produces,
    so they can be paired index-for-index. Missing value -> None / empty
    list.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            root = yaml.compose(fh)
    except (OSError, yaml.YAMLError):
        return []
    if root is None:
        return []

    out: list[dict] = []
    for key_node, value_node in getattr(root, "value", []):
        if getattr(key_node, "value", None) != "backups":
            continue
        for entry in getattr(value_node, "value", []):
            info = {"path": None, "exclude_lines": []}
            for k_node, v_node in getattr(entry, "value", []):
                key = getattr(k_node, "value", None)
                if key == "path":
                    info["path"] = v_node.start_mark.line + 1
                elif key == "exclude":
                    if isinstance(v_node, yaml.SequenceNode):
                        info["exclude_lines"] = [
                            it.start_mark.line + 1 for it in v_node.value
                        ]
                    else:
                        info["exclude_lines"] = [v_node.start_mark.line + 1]
            out.append(info)
    return out


def resolve_compression(cfg: dict, entry: dict) -> bool:
    """Whether the archive should be compressed (tar.zstd) or just packed (tar).

    Applies to the whole snapshot via default.compression but can be
    overridden per path with 'compression' in the backup entry. If the
    value is missing entirely, True is assumed, i.e. compressed
    (backward-compatible with previous behavior).
    """
    default_compress = cfg.get("default", {}).get("compression", True)
    return bool(entry.get("compression", default_compress))


def resolve_tool(cfg: dict, entry: dict) -> str:
    """Which compression tool (zstd or xz) to use.

    Applies to the whole snapshot via default.compression_tool but can be
    overridden per path with 'compression_tool'. Unknown/missing value =>
    zstd (backward-compatible).
    """
    default_tool = cfg.get("default", {}).get("compression_tool", DEFAULT_TOOL)
    tool = entry.get("compression_tool", default_tool)
    tool = str(tool).strip().lower() if tool else DEFAULT_TOOL
    return tool if tool in VALID_TOOLS else DEFAULT_TOOL


def resolve_threads(cfg: dict, entry: dict) -> int:
    """Number of CPU threads for compression (applies to both zstd and xz).

    Applies to the whole snapshot via default.threads but can be
    overridden per path with 'threads' in the backup entry. 0/empty/
    negative => all logical cores (-1), a positive N => exactly N threads.
    """
    default_threads = cfg.get("default", {}).get("threads")
    threads = entry.get("threads", default_threads)
    if threads is None:
        return THREADS_ALL
    try:
        threads = int(threads)
    except (TypeError, ValueError):
        return THREADS_ALL
    return threads if threads > 0 else THREADS_ALL


def resolve_split(cfg: dict, entry: dict) -> int:
    """Size limit per archive part, in bytes. 0 = no split.

    Applies to the whole snapshot via default.split but can be overridden
    per path with 'split'. The value is given in decimal GB (10^9 bytes),
    so split: 2 => exactly 2,000,000,000 bytes. 0/empty/negative => no
    split (a single archive is written).
    """
    default_split = cfg.get("default", {}).get("split")
    val = entry.get("split", default_split)
    if val is None:
        return 0
    try:
        gb = float(val)
    except (TypeError, ValueError):
        return 0
    if gb <= 0:
        return 0
    return int(round(gb * 1_000_000_000))


def resolve_compression_level(cfg: dict, entry: dict):
    """Compression level for a backup path.

    The level applies to the whole snapshot (via default.compression_level)
    but can be overridden per path with 'compression_level' in the backup
    entry. Empty/None/0 => the tool's default level.
    """
    default_level = cfg.get("default", {}).get("compression_level")
    level = entry.get("compression_level", default_level)
    return level


def normalize_excludes(raw) -> list[str]:
    """Allow 'exclude' to be either a string or a list in config.yml.

    A single string is treated as ONE path (not character-by-character).
    None/empty -> empty list.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    return [os.path.expanduser(e) for e in raw]


def is_excluded(path: str, excludes: list[str]) -> bool:
    p = norm(path)
    for ex in excludes:
        ex = norm(ex)
        if p == ex or p.startswith(ex + os.sep):
            return True
    return False


def list_subfolders(src: str, excludes: list[str]) -> list[str]:
    subs = []
    try:
        for name in sorted(os.listdir(src)):
            full = os.path.join(src, name)
            if os.path.isdir(full) and not os.path.islink(full) and not is_excluded(full, excludes):
                subs.append(name)
    except (OSError, PermissionError):
        pass
    return subs


def collect_files(base: str, excludes: list[str]) -> list[str]:
    """All regular files/symlinks under base (recursively), excludes filtered out."""
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not is_excluded(os.path.join(root, d), excludes)]
        for name in sorted(files):
            full = os.path.join(root, name)
            if not is_excluded(full, excludes):
                out.append(full)
    return out


def collect_root_files(src: str, excludes: list[str]) -> list[str]:
    """Only files directly in src (not in subfolders)."""
    out = []
    try:
        for name in sorted(os.listdir(src)):
            full = os.path.join(src, name)
            if os.path.isfile(full) and not is_excluded(full, excludes):
                out.append(full)
    except (OSError, PermissionError):
        pass
    return out


# --------------------------------------------------------------------------- #
# File info
# --------------------------------------------------------------------------- #
def file_info(abs_path: str, arcname: str) -> dict:
    """Minimal file info: only what is needed to later determine whether
    the file has changed on disk (size + mtime) compared to the stored data."""
    st = os.lstat(abs_path)
    return {
        "arcname": arcname,
        "source_path": abs_path,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": iso(st.st_mtime),
    }


# --------------------------------------------------------------------------- #
# Build an archive
# --------------------------------------------------------------------------- #
class _ProgressWriter:
    """Wrapper around the head of the pipeline that counts bytes as they stream.

    Progress is updated per written chunk (not per file), so the bar moves
    smoothly even when a single file is many GB. Bytes = uncompressed tar
    data fed into the compressor/split; matches total_bytes_to_process (the
    sum of the source files' sizes), plus a bit of tar overhead that gets
    clamped in the bar.
    """

    def __init__(self, fileobj, progress: "ProgressBar"):
        self._f = fileobj
        self._p = progress

    def write(self, data) -> int:
        self._f.write(data)
        n = len(data)
        self._p.update(n)
        return n

    def flush(self) -> None:
        if hasattr(self._f, "flush"):
            self._f.flush()


def _stream_tar(tar_fileobj, src_base, files, progress) -> tuple[list[dict], int]:
    """Writes all 'files' to an open tar stream and reports file info.

    Progress is counted byte-by-byte via _ProgressWriter during the actual
    streaming; here only size/mtime per file is collected for db/incremental.
    """
    files_info: list[dict] = []
    size_before = 0
    counting = _ProgressWriter(tar_fileobj, progress)
    tar = tarfile.open(mode="w|", fileobj=counting)
    try:
        for abs_path in files:
            arcname = os.path.relpath(abs_path, src_base)
            try:
                tar.add(abs_path, arcname=arcname, recursive=False)
            except (OSError, PermissionError):
                continue
            info = file_info(abs_path, arcname)
            files_info.append(info)
            size_before += info["size"]
    finally:
        tar.close()
    return files_info, size_before


def build_archive(
    archive_path: str,
    src_base: str,
    files: list[str],
    progress: ProgressBar,
    level,
    compress: bool = True,
    threads: int = THREADS_ALL,
    tool: str = DEFAULT_TOOL,
    split_bytes: int = 0,
) -> dict:
    """Creates an archive and returns a report.

    The flow is built as a pipeline (Python tar | compression | split)
    where each step is optional:

      compress=True   -> tar is streamed through the external 'tool'
                         (zstd/xz) with 'level' (interpreted per tool) and
                         'threads' CPU threads.
      compress=False  -> tar only (.tar); level/threads/tool are ignored.
      split_bytes>0   -> output is piped through 'split' into parts of
                         split_bytes bytes: archive_path.000,
                         archive_path.001, ...
      split_bytes==0  -> a single archive is written to archive_path.

    No uncompressed intermediate data is written to disk – everything is
    streamed.
    """
    start = time.monotonic()
    split_mode = split_bytes and split_bytes > 0

    # Clean up any leftover temp files from a previous run before we start.
    tmp_single = archive_path + ".part"
    tmp_prefix = archive_path + ".part."
    with contextlib.suppress(OSError):
        os.remove(tmp_single)
    for leftover in glob.glob(glob.escape(tmp_prefix) + "*"):
        with contextlib.suppress(OSError):
            os.remove(leftover)

    procs: list[tuple[str, subprocess.Popen]] = []
    single_fh = None
    files_info: list[dict] = []
    size_before = 0
    try:
        # ---- sink: split process or single file ----
        if split_mode:
            split_cmd = ["split", "-d", "-a", str(SPLIT_SUFFIX_LEN),
                         "-b", str(int(split_bytes)), "-", tmp_prefix]
            sink = subprocess.Popen(split_cmd, stdin=subprocess.PIPE)
            procs.append(("split", sink))
            sink_in = sink.stdin
        else:
            single_fh = open(tmp_single, "wb")
            sink_in = single_fh

        # ---- compression (optional) feeds the sink; tar feeds it ----
        if compress:
            comp = subprocess.Popen(compressor_cmd(tool, level, threads),
                                    stdin=subprocess.PIPE, stdout=sink_in)
            procs.append((tool, comp))
            # Only the compression step should keep the sink's stdin open.
            if split_mode:
                sink_in.close()
            else:
                single_fh.close()
                single_fh = None
            tar_target = comp.stdin
        else:
            tar_target = sink_in

        # ---- tar (Python) – writes into the head of the pipeline ----
        files_info, size_before = _stream_tar(tar_target, src_base, files, progress)
        tar_target.close()

        # ---- wait for and check all processes (bottom-up) ----
        for name, proc in reversed(procs):
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"{name} failed (code {rc}) for {archive_path}")
        if single_fh is not None:
            single_fh.close()
            single_fh = None

        # ---- move temp -> final names ----
        for old in existing_archive_parts(archive_path):
            with contextlib.suppress(OSError):
                os.remove(old)
        if split_mode:
            parts = sorted(glob.glob(glob.escape(tmp_prefix) + "*"))
            if len(parts) == 1:
                # Fits in a single part -> keep the base name without the .000 suffix.
                os.replace(parts[0], archive_path)
                outputs = [archive_path]
            else:
                final_parts = []
                for tmp in parts:
                    suffix = tmp[len(tmp_prefix):]
                    final = f"{archive_path}.{suffix}"
                    os.replace(tmp, final)
                    final_parts.append(final)
                outputs = final_parts
        else:
            os.replace(tmp_single, archive_path)
            outputs = [archive_path]
    except BaseException:
        # Clean up temp files and kill any remaining processes on error/abort.
        for _, proc in procs:
            if proc.poll() is None:
                proc.kill()
        if single_fh is not None:
            with contextlib.suppress(OSError):
                single_fh.close()
        with contextlib.suppress(OSError):
            os.remove(tmp_single)
        for leftover in glob.glob(glob.escape(tmp_prefix) + "*"):
            with contextlib.suppress(OSError):
                os.remove(leftover)
        raise

    size_after = sum(os.path.getsize(f) for f in outputs)
    duration = time.monotonic() - start
    saved = size_before - size_after
    comp_pct = (saved / size_before * 100.0) if size_before else 0.0
    ratio_pct = (size_after / size_before * 100.0) if size_before else 0.0

    return {
        "archive_path": archive_path,
        "compressed": compress,
        "tool": tool if compress else None,
        "split_bytes": int(split_bytes) if split_mode else 0,
        "part_count": len(outputs),
        "part_paths": outputs,
        "created_at": now_iso(),
        "duration_seconds": round(duration, 3),
        "size_before": size_before,
        "size_before_human": human(size_before),
        "size_after": size_after,
        "size_after_human": human(size_after),
        "saved_bytes": saved,
        "saved_human": human(saved),
        "compression_percent": round(comp_pct, 2),
        "ratio_percent": round(ratio_pct, 2),
        "file_count": len(files_info),
        "files": files_info,
    }


# --------------------------------------------------------------------------- #
# Preview + menu
# --------------------------------------------------------------------------- #
def build_plan(cfg: dict, config_path: str | None = None) -> list[dict]:
    """Works out what will be created per backup path."""
    output_root = os.path.expanduser(cfg["config"]["output_path"])
    line_info = backup_line_numbers(config_path) if config_path else []
    plan = []
    for i, entry in enumerate(cfg["backups"]):
        src = norm(os.path.expanduser(entry["path"]))
        excludes = normalize_excludes(entry.get("exclude"))
        out_dir = os.path.join(output_root, src.lstrip(os.sep))
        exists = os.path.isdir(src)
        subfolders = list_subfolders(src, excludes) if exists else []
        root_files = collect_root_files(src, excludes) if exists else []
        li = line_info[i] if i < len(line_info) else {}
        plan.append({
            "source": src,
            "excludes": excludes,
            "out_dir": out_dir,
            "exists": exists,
            "root_files": root_files,
            "subfolders": subfolders,
            "compress": resolve_compression(cfg, entry),
            "tool": resolve_tool(cfg, entry),
            "level": resolve_compression_level(cfg, entry),
            "threads": resolve_threads(cfg, entry),
            "split_bytes": resolve_split(cfg, entry),
            "path_line": li.get("path"),
            "exclude_lines": li.get("exclude_lines", []),
        })
    return plan


def preview(cfg: dict, plan: list[dict]) -> None:
    output_root = os.path.expanduser(cfg["config"]["output_path"])
    print("=" * 70)
    print("  BACKUP – PREVIEW")
    print("=" * 70)
    print(f"  Output path : {output_root}")
    print(f"  Path count  : {len(plan)}")
    print("-" * 70)
    for p in plan:
        print(f"\n  ● Source : {p['source']}")
        if not p["exists"]:
            print("    (!) Path does not exist on disk – skipping.")
            continue
        print(f"    Target : {p['out_dir']}")
        compress = p["compress"]
        ext = archive_ext(p["tool"], compress)
        if compress:
            lvl = clamp_level(p["tool"], p["level"])
            thr = "all cores" if p["threads"] < 0 else f"{p['threads']} threads"
            print(f"    Compr.: {p['tool']}  (level {lvl if lvl is not None else 'default'}, {thr})")
        else:
            print(f"    Compr.: no   (tar only)")
        if p["split_bytes"]:
            print(f"    Split : {gb_decimal(p['split_bytes'])} per part")
        if p["excludes"]:
            for ex in p["excludes"]:
                print(f"    Excl. : {ex}")
        split_note = "  (split)" if p["split_bytes"] else ""
        print(f"    Archives :")
        print(f"      - {'root' + ext:<24} ({len(p['root_files'])} files in root){split_note}")
        for sub in p["subfolders"]:
            print(f"      - {sub + ext:<24} (subfolder){split_note}")
        if not p["root_files"] and not p["subfolders"]:
            print("      (nothing to archive)")
    print("\n" + "=" * 70)


def validate_plan(plan: list[dict]) -> list[tuple[str, str]]:
    """Pre-flight check of the plan. Returns [('ERROR', message), ...]
    sorted in config line order. Each message starts with
    'Line N path' or 'Line N exclude' – identical format where only
    the word path/exclude differs."""
    # (line number, message) – sorted by line number before returning.
    raw: list[tuple[object, str]] = []
    home = os.path.expanduser("~")
    double_tilde = os.path.join(home, home.lstrip(os.sep))  # e.g. /home/x/home/x

    def loc(kind: str, line) -> str:
        """'Line 25 path' or just 'path' if the line number is missing."""
        return f"Line {line} {kind}" if line else kind

    seen: dict[str, int] = {}
    for i, p in enumerate(plan):
        src = p["source"]
        pline = p.get("path_line")
        exlines = p.get("exclude_lines", [])

        # Duplicate source
        if src in seen:
            raw.append((pline,
                f"{loc('path', pline)} is a duplicate of the same source: {src}"))
        else:
            seen[src] = i

        # Excludes: same wording as path (always runs – even if the source
        # is missing – so an error in the exclude isn't hidden by an error
        # in the path).
        for j, ex in enumerate(p["excludes"]):
            exline = exlines[j] if j < len(exlines) else None
            if not os.path.exists(norm(ex)):
                raw.append((exline, f"{loc('exclude', exline)} does not exist on disk: {ex}"))

        # Does the source exist? Is it a folder?
        if not os.path.exists(src):
            hint = "  (looks like a double '~' expansion – check the path)" \
                if src.startswith(double_tilde) else ""
            raw.append((pline, f"{loc('path', pline)} does not exist on disk: {src}{hint}"))
            continue
        if not os.path.isdir(src):
            raw.append((pline, f"{loc('path', pline)} is not a folder: {src}"))
            continue

        # Empty after exclusion
        if not p["root_files"] and not p["subfolders"]:
            raw.append((pline,
                f"{loc('path', pline)} is empty after exclusion – "
                f"nothing to archive: {src}"))

    # Sort by line order; entries without a line number go last.
    raw.sort(key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0))
    return [("ERROR", msg) for _, msg in raw]


def print_issues(issues: list[tuple[str, str]]) -> None:
    errors = [m for lvl, m in issues if lvl == "ERROR"]
    warns = [m for lvl, m in issues if lvl == "WARNING"]
    print("\n" + "-" * 70)
    print("  PATH CHECK")
    print("-" * 70)
    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for m in errors:
            print(f"    ✗ {m}")
    if warns:
        print(f"  WARNINGS ({len(warns)}):")
        for m in warns:
            print(f"    ! {m}")
    print("-" * 70)


def confirm(prompt: str) -> bool:
    while True:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False
        print("Answer y or n.")


# --------------------------------------------------------------------------- #
# db.json
# --------------------------------------------------------------------------- #
def load_db(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read().strip()
        if not data:
            return {"sessions": []}
        db = json.loads(data)
        if not isinstance(db, dict):
            db = {"sessions": []}
        db.setdefault("sessions", [])
        return db
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sessions": []}


def save_db(path: str, db: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Incremental: comparison against previous backups
# --------------------------------------------------------------------------- #
def iter_session_files(session: dict):
    """All file entries in a session (root + all subfolders)."""
    for backup in session.get("backups", []):
        root = backup.get("root")
        if root:
            yield from root.get("files", [])
        for sub in backup.get("subfolders", []):
            yield from sub.get("files", [])


def build_prior_index(db: dict) -> dict:
    """source_path -> {size, mtime} from the NEWEST session where the file appears.

    Sessions are walked newest->oldest; the first hit per source_path wins,
    so we always compare against the last time we actually packed the file
    (not against the first full backup)."""
    index: dict[str, dict] = {}
    for session in reversed(db.get("sessions", [])):
        for f in iter_session_files(session):
            sp = f.get("source_path")
            if sp and sp not in index:
                index[sp] = {"size": f.get("size"), "mtime": f.get("mtime")}
    return index


def is_changed(abs_path: str, prior_index: dict) -> bool:
    """True if the file is new or changed (size/mtime) compared to the latest backup."""
    prev = prior_index.get(abs_path)
    if prev is None:
        return True  # new file – never packed before
    try:
        st = os.lstat(abs_path)
    except OSError:
        return False
    if prev.get("size") is None or st.st_size != prev["size"]:
        return True
    if prev.get("mtime") is None or abs(st.st_mtime - prev["mtime"]) > MTIME_EPS:
        return True
    return False


def changed_files(files: list[str], prior_index: dict) -> list[str]:
    return [f for f in files if is_changed(f, prior_index)]


def incr_archive_name(base: str, stamp: str, tool: str = DEFAULT_TOOL,
                      compress: bool = True) -> str:
    """base = 'root' or subfolder name -> timestamped incremental archive name."""
    return f"{base}.incr-{stamp}{archive_ext(tool, compress)}"


# --------------------------------------------------------------------------- #
# Run the backup
# --------------------------------------------------------------------------- #
def total_bytes_to_process(plan: list[dict], mode: str = "full",
                           prior_index: dict | None = None) -> int:
    prior_index = prior_index or {}
    total = 0
    for p in plan:
        if not p["exists"]:
            continue
        root_files = p["root_files"]
        if mode == "incremental":
            root_files = changed_files(root_files, prior_index)
        for f in root_files:
            try:
                total += os.lstat(f).st_size
            except OSError:
                pass
        for sub in p["subfolders"]:
            sub_base = os.path.join(p["source"], sub)
            sub_files = collect_files(sub_base, p["excludes"])
            if mode == "incremental":
                sub_files = changed_files(sub_files, prior_index)
            for f in sub_files:
                try:
                    total += os.lstat(f).st_size
                except OSError:
                    pass
    return total


def run(cfg: dict, plan: list[dict], db_path: str,
        mode: str = "full", prior_index: dict | None = None) -> None:
    policy = OverwritePolicy()
    prior_index = prior_index or {}
    incremental = mode == "incremental"

    total = total_bytes_to_process(plan, mode, prior_index)
    progress = ProgressBar(total)
    session_start = time.monotonic()

    session_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    # Shared timestamp for all incremental archives in this run.
    incr_stamp = session_id.rsplit("-", 1)[0]

    session = {
        "session_id": session_id,
        "session_type": mode,
        "started_at": now_iso(),
        "finished_at": None,
        "duration_seconds": None,
        "output_path": os.path.expanduser(cfg["config"]["output_path"]),
        # Don't re-save the 'backups' list here – it's already reflected in
        # session["backups"] below.
        "config": {k: v for k, v in cfg.items() if k != "backups"},
        "backups": [],
        "totals": {},
    }

    grand_before = grand_after = grand_saved = 0
    grand_files = grand_archives = 0

    for p in plan:
        if not p["exists"]:
            continue
        os.makedirs(p["out_dir"], exist_ok=True)

        backup_report = {
            "source_path": p["source"],
            "excludes": p["excludes"],
            "root": None,
            "subfolders": [],
        }

        # --- root archive ---
        compress = p["compress"]
        tool = p["tool"]
        split_bytes = p["split_bytes"]
        root_files = p["root_files"]
        if incremental:
            root_files = changed_files(root_files, prior_index)
        root_name = (incr_archive_name("root", incr_stamp, tool, compress)
                     if incremental else root_archive_name(tool, compress))
        root_archive = os.path.join(p["out_dir"], root_name)
        if root_files:
            if existing_archive_parts(root_archive) and not policy.allow(root_archive):
                print(f"  Skipping {root_archive}")
            else:
                progress.set_label(f"{os.path.basename(p['source'])}/root")
                rep = build_archive(root_archive, p["source"], root_files,
                                    progress, p["level"], compress, p["threads"],
                                    tool, split_bytes)
                rep["type"] = "root"
                rep["archive_name"] = root_name
                backup_report["root"] = rep
                grand_before += rep["size_before"]
                grand_after += rep["size_after"]
                grand_saved += rep["saved_bytes"]
                grand_files += rep["file_count"]
                grand_archives += 1

        # --- one archive per subfolder ---
        for sub in p["subfolders"]:
            sub_base = os.path.join(p["source"], sub)
            sub_files = collect_files(sub_base, p["excludes"])
            if incremental:
                sub_files = changed_files(sub_files, prior_index)
            sub_name = (incr_archive_name(sub, incr_stamp, tool, compress)
                        if incremental else sub + archive_ext(tool, compress))
            archive = os.path.join(p["out_dir"], sub_name)
            if not sub_files:
                continue
            if existing_archive_parts(archive) and not policy.allow(archive):
                print(f"  Skipping {archive}")
                continue
            progress.set_label(f"{os.path.basename(p['source'])}/{sub}")
            rep = build_archive(archive, p["source"], sub_files,
                                progress, p["level"], compress, p["threads"],
                                tool, split_bytes)
            rep["type"] = "subfolder"
            rep["subfolder_name"] = sub
            rep["archive_name"] = sub_name
            backup_report["subfolders"].append(rep)
            grand_before += rep["size_before"]
            grand_after += rep["size_after"]
            grand_saved += rep["saved_bytes"]
            grand_files += rep["file_count"]
            grand_archives += 1

        session["backups"].append(backup_report)

    progress.finish()

    session["finished_at"] = now_iso()
    session["duration_seconds"] = round(time.monotonic() - session_start, 3)
    session["totals"] = {
        "archive_count": grand_archives,
        "file_count": grand_files,
        "size_before": grand_before,
        "size_before_human": human(grand_before),
        "size_after": grand_after,
        "size_after_human": human(grand_after),
        "saved_bytes": grand_saved,
        "saved_human": human(grand_saved),
        "compression_percent": round((grand_saved / grand_before * 100.0), 2) if grand_before else 0.0,
    }

    db = load_db(db_path)
    db["sessions"].append(session)
    save_db(db_path, db)

    # --- summary ---
    t = session["totals"]
    print("\n" + "=" * 70)
    print("  DONE" + ("  (incremental)" if incremental else "  (full)"))
    print("=" * 70)
    if incremental and t["archive_count"] == 0:
        print("  No files have changed since the last backup – nothing was packed.")
    print(f"  Archives created : {t['archive_count']}")
    print(f"  Files            : {t['file_count']}")
    print(f"  Before           : {t['size_before_human']}")
    print(f"  After            : {t['size_after_human']}")
    print(f"  Saved            : {t['saved_human']}  ({t['compression_percent']} %)")
    print(f"  Time             : {format_duration(session['duration_seconds'])}")
    print(f"  Session saved in {db_path} (id {session['session_id']})")
    print("=" * 70)


# --------------------------------------------------------------------------- #
# Job type (full / incremental) + warnings
# --------------------------------------------------------------------------- #
def describe_latest(session: dict) -> None:
    t = session.get("totals", {})
    print("-" * 70)
    print("  A previous job has already run:")
    print(f"    Last run        : {session.get('session_id')}"
          f"  ({session.get('session_type', 'full')})")
    print(f"    Started         : {session.get('started_at')}")
    print(f"    Scope           : {t.get('archive_count', 0)} archives, "
          f"{t.get('file_count', 0)} files, {t.get('size_before_human', '?')}")
    print("-" * 70)


def startup_menu() -> str:
    """Startup choice: view history, run backup, or quit."""
    print("=" * 70)
    print("  AWS-BACKUP")
    print("=" * 70)
    print("  [H] View history")
    print("  [R] Run backup")
    print("  [Q] Quit")
    while True:
        ans = input("Choice: ").strip().lower()
        if ans in ("h", "history"):
            return "history"
        if ans in ("r", "run", "backup"):
            return "backup"
        if ans in ("q", "quit"):
            return "quit"
        print("Answer H (history), R (run backup) or Q (quit).")


def choose_mode() -> str:
    while True:
        ans = input("Run [F]ull or [I]ncremental backup? : ").strip().lower()
        if ans in ("f", "full"):
            return "full"
        if ans in ("i", "incremental", "incr"):
            return "incremental"
        print("Answer F (full) or I (incremental).")


def existing_archive_dirs(plan: list[dict]) -> list[tuple[str, int]]:
    """Target folders that already contain archive files -> (folder, count)."""
    hits = []
    for p in plan:
        if not p["exists"]:
            continue
        d = p["out_dir"]
        if not os.path.isdir(d):
            continue
        found = [f for f in os.listdir(d) if is_archive_name(f)]
        if found:
            hits.append((d, len(found)))
    return hits


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="tar.zstd backup from config.yml")
    ap.add_argument("-c", "--config", default="config.yml")
    ap.add_argument("-d", "--db", default="db.json")
    ap.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    mode_grp = ap.add_mutually_exclusive_group()
    mode_grp.add_argument("--full", action="store_const", dest="mode", const="full",
                          help="Force full backup (otherwise interactive prompt)")
    mode_grp.add_argument("--incremental", action="store_const", dest="mode",
                          const="incremental", help="Force incremental backup")
    args = ap.parse_args()

    # Startup menu (interactive only, without forcing flags).
    if not args.yes and args.mode is None and sys.stdin.isatty():
        import history
        while True:
            choice = startup_menu()
            if choice == "quit":
                return
            if choice == "history":
                # Esc/← at the top level -> back to the menu; Q -> quit the app.
                if history.browse(args.db) == "quit":
                    return
                continue
            break  # "backup" -> continue below

    cfg = load_config(args.config)
    plan = build_plan(cfg, args.config)
    preview(cfg, plan)

    # --- Pre-flight: check all paths before anything runs ---
    issues = validate_plan(plan)
    runnable = any(p["exists"] for p in plan)
    if issues:
        print_issues(issues)
        if not runnable:
            print("\nNo runnable paths remain – aborting.")
            return
        if not args.yes and not confirm("Continue despite the above?"):
            print("Aborted.")
            return
    else:
        print("\n  ✓ All paths checked – no problems found.")
        if not runnable:
            print("\nNone of the configured paths exist – aborting.")
            return

    # --- Determine job type: full or incremental ---
    db = load_db(args.db)
    sessions = db.get("sessions", [])
    if not sessions:
        # Very first run ever -> always full.
        mode = "full"
        print("\nNo previous jobs found – running a FULL backup.")
    elif args.mode:
        mode = args.mode
    else:
        describe_latest(sessions[-1])
        if args.yes:
            # Non-interactive without a flag: incremental as the safe default.
            mode = "incremental"
            print("  (-y without --full/--incremental: choosing incremental)")
        else:
            mode = choose_mode()

    prior_index = build_prior_index(db) if mode == "incremental" else {}

    # --- Warn if the target folders already contain archives ---
    hits = existing_archive_dirs(plan)
    if hits:
        print("\n(!) The output folder already contains archives:")
        for d, n in hits:
            print(f"      {d}  ({n} archives)")
        if not args.yes and not confirm("Continue anyway?"):
            print("Aborted.")
            return

    if not args.yes and not confirm(f"\nRun {mode.upper()} backup with this configuration?"):
        print("Aborted.")
        return

    run(cfg, plan, args.db, mode, prior_index)


if __name__ == "__main__":
    main()
