# aws-backup

Incremental backups for **cold storage** such as AWS Glacier Deep Archive.

Make compressed archives of your folders, upload them wherever you like, then
**delete them locally**. A small local log records every file that went into
every archive. Next time you run it, only new and changed files are packed.
You never need to read the archives back to work out what to upload.

## Why

Deep-archive storage is very cheap, but it has two catches:

- **Minimum storage time.** Glacier Deep Archive bills at least 180 days per
  object, so uploading a new backup every week costs a lot.
- **No quick access.** Getting data back takes hours and costs money, so you
  can't just look inside the archive to see what's already there.

This tool is built around those limits:

- **Run it rarely** (e.g. twice a year). It works out what changed since the
  last run.
- **The log is the source of truth.** `db.json` stores the size and modification
  time of every archived file, so incremental runs need no access to old
  archives.
- **No manual zip/WinRAR work.** You don't have to keep track of what was
  already uploaded.
- **One archive per subfolder**, so a restore only needs the parts you want.
- **Streams `tar | zstd/xz | split`**, so no uncompressed copy is written to
  disk. Large archives are split into upload-friendly parts.

## Workflow

```
1. Full run          ->  archives in output_path  ->  upload  ->  delete locally
2. ~6 months later   ->  incremental run          ->  upload  ->  delete locally
3. Repeat step 2
```

Incremental archives are named `<folder>.incr-<timestamp>.tar.zstd`, so
they sit alongside the full archives in your bucket without overwriting
anything.

## Quick start

See [Requirements](#requirements) first.

```bash
git clone https://github.com/<you>/aws-backup.git
cd aws-backup
pip install PyYAML
```

Create `config.yml`:

```yaml
config:
  output_path: ~/backup-out

backups:
  - path: ~/Documents
  - path: ~/Pictures
    exclude: "~/Pictures/tmp"
```

That's all you need. By default it uses zstd compression, all CPU cores, and
no splitting. See [config.example.yml](config.example.yml) for every option
(xz, compression level, threads, split size, per-path overrides).

Run it:

```bash
python backup.py
```

The first run is always a **full** backup. After that it asks whether to run a
full or incremental backup. It shows a preview and asks you to confirm
before it writes anything.

## Output

The source path is recreated under `output_path`, with one archive per subfolder:

```
~/backup-out/home/you/Documents/
├── root.tar.zstd              # files directly in Documents/
├── Invoices.tar.zstd
├── Photos.tar.zstd.000        # split into parts
├── Photos.tar.zstd.001
└── Invoices.incr-20270315-101500.tar.zstd
```

Upload the contents to your storage, for example:

```bash
aws s3 sync ~/backup-out s3://my-bucket/backup --storage-class DEEP_ARCHIVE
```

## Browse the history

```bash
python history.py
```

A terminal viewer for `db.json`. You can drill down from runs to archives to
individual files. It shows sizes, compression ratios, and space saved. Use it
to answer "which archive has that file?" without touching cold storage.

## Command line

| Flag | Description |
|---|---|
| `-c, --config` | Config file (default `config.yml`) |
| `-d, --db` | History log (default `db.json`) |
| `--full` / `--incremental` | Pick the mode without being asked |
| `-y, --yes` | Skip confirmations (defaults to incremental), for cron |

## Important

**Back up `db.json`.** It's small, and it's the only record of what is in your
archives. Keep a copy next to the archives in your bucket, and another
somewhere you can reach quickly.

## Requirements

| | |
|---|---|
| **Python** | 3.9 or newer |
| **Python packages** | `PyYAML` (only needed by `backup.py`; `history.py` uses only the standard library) |
| **Compression** | `zstd` (default) and/or `xz` command-line tools on your PATH |
| **Splitting** | `split` from coreutils (only if `split` is set in the config) |

Install the tools:

```bash
# Debian / Ubuntu
sudo apt install zstd xz-utils
# Fedora
sudo dnf install zstd xz
# Arch
sudo pacman -S zstd xz
# macOS (Homebrew)
brew install zstd xz
```

### Operating systems

| OS | Status |
|---|---|
| **Linux** | Recommended. Developed and tested here. |
| **macOS** | Should work (same tools via Homebrew) but not tested. |
| **Windows** | Not supported natively: there's no `split` command, and Python's `curses` module (used by the history viewer) isn't included. Use **WSL**. It runs as on Linux and can reach your Windows drives under `/mnt/c/...`. |
