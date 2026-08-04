#!/usr/bin/env python3
"""
Dead Reckoning
ArcGIS Pro project backup and index-integrity monitor.

Named for the navigational method of recovering your position from the last
known good fix, which is exactly what this does for an .aprx.

WHAT IT DOES
    1. Scans one or more folders for .aprx files.
    2. Opens each one as a ZIP archive and validates Index.json without
       launching ArcGIS Pro. Flags the thousands-separator corruption that
       makes maps silently fail to open.
    3. Copies each project into a timestamped snapshot folder, tagging the
       filename OK or SUSPECT so you always know which restore point is good.
    4. Bundles the snapshot into a single .zip.
    5. Prunes old snapshots according to a retention setting.
    6. Optionally exports every map to .mapx (needs arcpy).
    7. Can repair a corrupt .aprx in place-safe fashion, writing a new file.

USAGE
    GUI:        python dead_reckoning.py
    Headless:   python dead_reckoning.py --run
    Repair:     python dead_reckoning.py --repair "C:\\path\\Broken.aprx"
    Check only: python dead_reckoning.py --check "C:\\projects"

Requires Python 3.9+. No third-party packages. arcpy optional.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
import datetime as _dt  # aliased: arcpy injects `datetime` into __main__
from pathlib import Path

APP_NAME = "Dead Reckoning"
APP_VERSION = "1.0"
WORK_DIRNAME = "_deadreckoning"

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------


def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = Path(base) / "DeadReckoning"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


DEFAULT_CONFIG = {
    "sources": [],
    "destination": "",
    "recursive": True,
    "keep_snapshots": 7,
    "make_zip": True,
    "keep_loose_copies": True,
    "export_mapx": False,
    "theme": "slate",
    "exclude_patterns": [
        "~$",
        "backup",
        "_deadreckoning",
        "recover",
        "extractpackage",
        "unpacked",
        "scratch",
        "temp",
        "appdata",
    ],
}


def norm_path(p) -> str:
    """Normalize separators. Windows dialogs return forward slashes, which
    ShellExecute and some APIs reject on UNC paths such as //server/share."""
    p = str(p or "").strip().strip('"')
    if not p:
        return ""
    try:
        return os.path.normpath(p)
    except Exception:
        return p


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Integrity inspection
# ----------------------------------------------------------------------------

HEALTHY = "HEALTHY"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
UNREADABLE = "UNREADABLE"

STATUS_ORDER = {CRITICAL: 0, UNREADABLE: 1, WARNING: 2, HEALTHY: 3}

# Matches a number written with thousands separators sitting in JSON value
# position, i.e. immediately after a colon. This is the exact defect seen in
# the 31 July incident: "NumberOfNodes" : 1,376
SEPARATOR_IN_VALUE = re.compile(r'(?<=:)(\s*)(-?\d{1,3}(?:,\d{3})+)(?=\s*[,\}\]\r\n])')

NUMBER_OF_NODES = re.compile(r'"NumberOfNodes"\s*:\s*"?(-?[\d,]+)"?')


@dataclass
class ProjectHealth:
    path: str
    name: str
    size_bytes: int = 0
    modified: str = ""
    status: str = UNREADABLE
    headline: str = ""
    details: list = field(default_factory=list)
    node_count: int = 0
    max_child_id: int = 0
    map_count: int = 0
    item_count: int = 0
    separator_hits: int = 0
    index_error: str = ""

    @property
    def integrity_ratio(self) -> float:
        """max child id / node count. Near 1.0 is healthy. A low value means
        four-digit ids were split by commas and silently truncated."""
        if self.node_count <= 0:
            return 0.0
        return min(1.0, self.max_child_id / float(self.node_count))


def _read_member(zf: zipfile.ZipFile, wanted: str):
    """Case-insensitive member lookup. Returns (name, text) or (None, None)."""
    for n in zf.namelist():
        if n.lower() == wanted.lower() or n.lower().endswith("/" + wanted.lower()):
            raw = zf.read(n)
            for enc in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
                try:
                    return n, raw.decode(enc)
                except (UnicodeDecodeError, UnicodeError):
                    continue
            return n, raw.decode("utf-8", errors="replace")
    return None, None


def _walk_values(obj, key):
    """Yield every value stored under `key` anywhere in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _walk_values(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_values(item, key)


def _child_ids(index_obj) -> int:
    biggest = 0
    for v in _walk_values(index_obj, "ChildNodeIds"):
        if isinstance(v, str):
            tokens = re.split(r"[;,|\s]+", v)
        elif isinstance(v, list):
            tokens = [str(t) for t in v]
        else:
            tokens = [str(v)]
        for t in tokens:
            t = t.strip()
            if t.isdigit():
                biggest = max(biggest, int(t))
    return biggest


def inspect_aprx(path) -> ProjectHealth:
    p = Path(path)
    h = ProjectHealth(path=str(p), name=p.stem)

    try:
        st = p.stat()
        h.size_bytes = st.st_size
        h.modified = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError as e:
        h.headline = f"Cannot stat file: {e}"
        return h

    try:
        with zipfile.ZipFile(p) as zf:
            bad = zf.testzip()
            if bad:
                h.status = CRITICAL
                h.headline = f"Archive corrupt at {bad}"
                return h

            idx_name, idx_text = _read_member(zf, "Index.json")
            gis_name, gis_text = _read_member(zf, "GISProject.json")

            if idx_text is None:
                h.status = WARNING
                h.headline = (
                    "No stored index — usually an unpacked package or a project "
                    "saved with no open maps"
                )
                h.details.append(
                    "This is not the corruption signature. A project with no "
                    "Index.json opens normally; there is simply no saved view "
                    "state to restore."
                )
                if gis_text:
                    try:
                        gis = json.loads(gis_text)
                        types = list(_walk_values(gis, "itemType"))
                        h.item_count = len(types)
                        h.map_count = sum(1 for t in types if str(t).lower() == "map")
                    except json.JSONDecodeError:
                        pass
                return h

            h.separator_hits = len(SEPARATOR_IN_VALUE.findall(idx_text))

            index_obj = None
            try:
                index_obj = json.loads(idx_text)
            except json.JSONDecodeError as e:
                h.index_error = f"line {e.lineno}, col {e.colno}: {e.msg}"

            m = NUMBER_OF_NODES.search(idx_text)
            if m:
                try:
                    h.node_count = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass

            if index_obj is not None:
                h.max_child_id = _child_ids(index_obj)

            if gis_text:
                try:
                    gis = json.loads(gis_text)
                    types = list(_walk_values(gis, "itemType"))
                    h.item_count = len(types)
                    h.map_count = sum(1 for t in types if str(t).lower() == "map")
                except json.JSONDecodeError:
                    h.details.append("GISProject.json did not parse")

            # -- verdict ---------------------------------------------------
            if index_obj is None:
                h.status = CRITICAL
                if h.separator_hits:
                    h.headline = (
                        f"Index.json invalid — {h.separator_hits} thousands-separator "
                        f"hits ({h.index_error})"
                    )
                    h.details.append(
                        "This is the known serialization defect. Use Repair, "
                        "then Save As in ArcGIS Pro to rebuild the index."
                    )
                else:
                    h.headline = f"Index.json invalid — {h.index_error}"
            elif h.separator_hits:
                h.status = WARNING
                h.headline = (
                    f"Index.json parses but contains {h.separator_hits} "
                    "separator-formatted numbers"
                )
            elif h.node_count >= 1000 and h.max_child_id and h.max_child_id < 1000:
                h.status = WARNING
                h.headline = (
                    f"Child id ceiling looks truncated — highest is "
                    f"{h.max_child_id} across {h.node_count} nodes"
                )
                h.details.append(
                    "Four-digit ids may have been split on commas. Re-save the "
                    "project in Pro to rebuild the index cleanly."
                )
            else:
                h.status = HEALTHY
                bits = []
                if h.map_count:
                    bits.append(f"{h.map_count} maps")
                bits.append(f"{h.size_bytes / 1048576:.1f} MB")
                h.headline = " · ".join(bits)

    except zipfile.BadZipFile:
        h.status = CRITICAL
        h.headline = "Not a readable ZIP archive — file may be truncated"
    except PermissionError:
        h.status = UNREADABLE
        h.headline = "Locked — the project is probably open in ArcGIS Pro"
    except Exception as e:
        h.status = UNREADABLE
        h.headline = f"{type(e).__name__}: {e}"

    return h


# ----------------------------------------------------------------------------
# Repair
# ----------------------------------------------------------------------------


def repair_aprx(src, dst=None):
    """Rewrite Index.json with thousands separators stripped from numeric
    values. Never modifies the source. Returns (ok, message, out_path)."""
    src = Path(src)
    if dst is None:
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
        dst = src.with_name(f"{src.stem}_repaired_{stamp}.aprx")
    dst = Path(dst)

    try:
        with zipfile.ZipFile(src) as zf:
            idx_name, idx_text = _read_member(zf, "Index.json")
            if idx_text is None:
                return False, "No Index.json found in the archive.", None

            fixed, count = SEPARATOR_IN_VALUE.subn(
                lambda m: m.group(1) + m.group(2).replace(",", ""), idx_text
            )
            if count == 0:
                return False, "No thousands-separator values found — nothing to repair.", None

            try:
                json.loads(fixed)
            except json.JSONDecodeError as e:
                return (
                    False,
                    f"Still invalid after {count} replacements (line {e.lineno}, "
                    f"col {e.colno}: {e.msg}). Manual inspection needed.",
                    None,
                )

            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
                for item in zf.infolist():
                    data = zf.read(item.filename)
                    if item.filename == idx_name:
                        data = fixed.encode("utf-8")
                    info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                    info.compress_type = item.compress_type
                    info.external_attr = item.external_attr
                    out.writestr(info, data)

        return (
            True,
            f"Repaired {count} values. Open {dst.name} in ArcGIS Pro, confirm the "
            "maps load, then use Save As so Pro rebuilds the index from scratch.",
            dst,
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


# ----------------------------------------------------------------------------
# Backup engine
# ----------------------------------------------------------------------------


GUID_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.aprx$", re.I
)


def discover(cfg, emit=None, should_cancel=None) -> list:
    """Walk the source folders for .aprx files, pruning excluded directories
    and the backup destination as we go rather than filtering afterwards.
    On a network share that difference is minutes, not milliseconds."""
    found = []
    excludes = [x.lower() for x in cfg.get("exclude_patterns", []) if x]
    dest = norm_path(cfg.get("destination", "")).lower()

    for root in cfg.get("sources", []):
        rp = Path(norm_path(root))
        if not rp.exists():
            if emit:
                emit(f"Source folder not found: {rp}")
            continue
        if emit:
            emit(f"Searching {rp} …")

        if not cfg.get("recursive", True):
            found.extend(rp.glob("*.aprx"))
            continue

        seen_dirs = 0
        for dirpath, dirnames, filenames in os.walk(rp):
            seen_dirs += 1
            if emit and seen_dirs % 150 == 0:
                emit(f"Searching — {seen_dirs} folders, {len(found)} projects so far…")
            if should_cancel and should_cancel():
                dirnames[:] = []
                if emit:
                    emit("Search cancelled.")
                return sorted(set(found), key=lambda x: x.name.lower())
            low = dirpath.lower()
            if dest and (low == dest or low.startswith(dest + os.sep)):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames if not any(x in d.lower() for x in excludes)
            ]
            for fn in filenames:
                if not fn.lower().endswith(".aprx"):
                    continue
                if GUID_NAME.match(fn):
                    continue
                if any(x in fn.lower() for x in excludes):
                    continue
                found.append(Path(dirpath) / fn)

    if emit:
        emit(f"Found {len(found)} project files.")
    return sorted(set(found), key=lambda x: x.name.lower())


def snapshot_target(snap: Path, src: Path, cfg, tag: str) -> Path:
    """Build a collision-proof destination path. Projects sharing a filename
    are common (copies of a tracker, per-year folders), so the snapshot
    mirrors the source folder structure rather than flattening it."""
    rel = None
    for root in cfg.get("sources", []):
        rp = Path(norm_path(root))
        try:
            rel = src.relative_to(rp)
            break
        except ValueError:
            continue

    if rel is None:
        rel = Path(src.name)

    parent = rel.parent
    stem = src.stem
    target = snap / "files" / parent / f"{stem}__{tag}.aprx"

    # Belt and braces: if two source roots produce the same relative path.
    n = 2
    while target.exists():
        target = snap / "files" / parent / f"{stem}__{tag}_{n}.aprx"
        n += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def list_snapshots(cfg) -> list:
    """Newest first. Returns [(display_name, snapshot_dir, manifest_dict)]."""
    dest = Path(norm_path(cfg.get("destination", "")))
    snap_root = dest / "snapshots"
    if not snap_root.is_dir():
        return []
    out = []
    for d in sorted(snap_root.glob("*"), key=lambda x: x.name, reverse=True):
        if not d.is_dir():
            continue
        man_file = d / "manifest.json"
        try:
            man = json.loads(man_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        label = d.name + ("  (partial run)" if d.name.endswith("_PARTIAL") else "")
        out.append((label, d, man))
    return out


def restore_project(snapshot_dir: Path, record: dict, replace: bool = False):
    """Copy a project out of a snapshot.

    replace=False  writes <name>_restored_<snapshot>.aprx beside the original
                   and touches nothing else. This is the default.
    replace=True   overwrites the original, but only after copying the current
                   file to <name>_before_restore_<timestamp>.aprx first.

    Returns (ok, message, written_path).
    """
    rel = record.get("backup")
    if not rel:
        return False, "That project was not copied in this snapshot.", None

    src = snapshot_dir / rel
    if not src.exists():
        return False, f"Missing from the snapshot: {rel}", None

    original = Path(record.get("path", ""))
    target_dir = original.parent if original.parent.exists() else snapshot_dir
    stem = original.stem or Path(rel).stem.split("__")[0]

    if not replace:
        tag = snapshot_dir.name
        out = target_dir / f"{stem}_restored_{tag}.aprx"
        n = 2
        while out.exists():
            out = target_dir / f"{stem}_restored_{tag}_{n}.aprx"
            n += 1
        try:
            shutil.copy2(src, out)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", None
        return (
            True,
            f"Restored alongside the original as:\n\n{out.name}\n\nNothing was "
            "overwritten. Open it, confirm the maps load, then swap it in "
            "yourself.",
            out,
        )

    if not original.exists():
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, original)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", None
        return True, f"The original was missing, so it was recreated at:\n\n{original}", original

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safety = original.with_name(f"{stem}_before_restore_{stamp}.aprx")
    try:
        shutil.copy2(original, safety)
    except Exception as e:
        return False, f"Could not make the safety copy, so nothing was changed:\n{e}", None

    try:
        shutil.copy2(src, original)
    except Exception as e:
        return False, f"Copy failed; the original is untouched and a safety copy exists.\n{e}", None

    return (
        True,
        f"Replaced the original.\n\nThe previous file was saved as:\n{safety.name}\n\n"
        "If this is not the version you wanted, that file is your undo.",
        original,
    )


def work_dir(cfg) -> Path:
    d = Path(norm_path(cfg["destination"])) / WORK_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_history(cfg) -> list:
    f = work_dir(cfg) / "history.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def append_history(cfg, record) -> None:
    f = work_dir(cfg) / "history.json"
    hist = load_history(cfg)
    hist.append(record)
    hist = hist[-400:]
    try:
        f.write_text(json.dumps(hist, indent=1), encoding="utf-8")
    except Exception:
        pass


def log_line(cfg, text) -> None:
    try:
        with open(work_dir(cfg) / "activity.log", "a", encoding="utf-8") as fh:
            fh.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
    except Exception:
        pass


_ARCPY = {"module": None, "tried": False}


def _load_arcpy(emit):
    """In-process arcpy import. Only used as a fallback when the subprocess
    worker cannot be started, because this blocks the GIL for the duration of
    the import and will freeze any GUI in the same process."""
    if _ARCPY["tried"]:
        return _ARCPY["module"]
    _ARCPY["tried"] = True
    try:
        import arcpy  # noqa

        _ARCPY["module"] = arcpy
    except Exception as e:
        emit(f"arcpy unavailable ({type(e).__name__}) — skipping .mapx export.")
    return _ARCPY["module"]


def _export_maps_with(arcpy, aprx_copy, out_dir, log):
    """Shared export body, used by both the worker process and the fallback."""
    n = 0
    proj = arcpy.mp.ArcGISProject(str(aprx_copy))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for m in proj.listMaps():
        safe = re.sub(r'[<>:"/\\|?*]', "_", m.name).strip() or f"map_{n}"
        try:
            m.exportToMAPX(str(Path(out_dir) / f"{safe}.mapx"))
            n += 1
        except Exception as e:
            log(f"  map '{m.name}' failed: {e}")
    del proj
    return n


class MapxWorker:
    """Runs arcpy in a child process.

    Importing arcpy takes tens of seconds and holds the GIL throughout, so
    doing it in-process freezes the window even from a background thread. A
    child process has its own interpreter and its own GIL, so the parent stays
    fully responsive. The process is started once per run and reused, because
    paying arcpy's startup cost per project would be far worse than one wait.
    """

    def __init__(self, emit):
        self.emit = emit
        self.proc = None
        self.ready = False
        self.failed = False

    def start(self):
        if self.proc is not None or self.failed:
            return self.ready

        script = os.path.abspath(__file__)
        exe = sys.executable
        cmd = [exe, script, "--mapx-worker"]

        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "bufsize": 1,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        self.emit("Starting arcpy helper — patience is a virtue")
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
            line = self.proc.stdout.readline()
            reply = json.loads(line) if line else {}
        except Exception as e:
            self.emit(f"Could not start the arcpy helper ({type(e).__name__}).")
            self.failed = True
            self.proc = None
            return False

        if reply.get("ready"):
            self.ready = True
            self.emit("arcpy helper ready.")
        else:
            self.failed = True
            self.emit(f"arcpy helper unavailable: {reply.get('error', 'unknown')}")
            self.stop()
        return self.ready

    def export(self, aprx_copy, out_dir):
        if not self.ready and not self.start():
            return self._fallback(aprx_copy, out_dir)
        try:
            payload = json.dumps({"aprx": str(aprx_copy), "out": str(out_dir)})
            self.proc.stdin.write(payload + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("helper stopped responding")
            reply = json.loads(line)
        except Exception as e:
            self.emit(f"  arcpy helper error: {type(e).__name__}: {e}")
            self.stop()
            self.failed = True
            return 0

        for note in reply.get("notes", []):
            self.emit(note)
        if not reply.get("ok"):
            self.emit(f"  mapx export failed: {reply.get('error')}")
            return 0
        return int(reply.get("count", 0))

    def _fallback(self, aprx_copy, out_dir):
        arcpy = _load_arcpy(self.emit)
        if arcpy is None:
            return 0
        try:
            return _export_maps_with(arcpy, aprx_copy, out_dir, self.emit)
        except Exception as e:
            self.emit(f"  mapx export failed: {type(e).__name__}: {e}")
            return 0

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self.ready = False


def mapx_worker_main() -> int:
    """Child process entry point. Reads one JSON request per line."""
    try:
        import arcpy  # noqa
    except Exception as e:
        sys.stdout.write(json.dumps({"ready": False, "error": str(e)}) + "\n")
        sys.stdout.flush()
        return 1

    sys.stdout.write(json.dumps({"ready": True}) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        notes = []
        try:
            req = json.loads(line)
            count = _export_maps_with(
                arcpy, req["aprx"], req["out"], lambda m: notes.append(m)
            )
            reply = {"ok": True, "count": count, "notes": notes}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}", "notes": notes}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


def prune(cfg, emit) -> None:
    keep = int(cfg.get("keep_snapshots", 30))
    if keep <= 0:
        return
    dest = Path(norm_path(cfg["destination"]))

    all_dirs = [d for d in (dest / "snapshots").glob("*") if d.is_dir()]
    partial = sorted([d for d in all_dirs if d.name.endswith("_PARTIAL")], key=lambda d: d.name)
    snaps = sorted([d for d in all_dirs if not d.name.endswith("_PARTIAL")], key=lambda d: d.name)

    # Cancelled runs are never counted as restore points, but they should not
    # pile up either.
    for old in partial[:-2] if len(partial) > 2 else []:
        try:
            shutil.rmtree(old)
            emit(f"removed partial run {old.name}")
        except Exception:
            pass

    for old in snaps[:-keep] if len(snaps) > keep else []:
        try:
            shutil.rmtree(old)
            emit(f"pruned snapshot {old.name}")
        except Exception as e:
            emit(f"could not prune {old.name}: {e}")

    zips = sorted((dest / "archives").glob("*.zip"), key=lambda f: f.name)
    for old in zips[:-keep] if len(zips) > keep else []:
        try:
            old.unlink()
            emit(f"pruned archive {old.name}")
        except Exception as e:
            emit(f"could not prune {old.name}: {e}")


def run_backup(cfg, emit=print, progress=None, should_cancel=None) -> dict:
    if not cfg.get("destination"):
        raise ValueError("No destination folder configured.")
    if not cfg.get("sources"):
        raise ValueError("No source folders configured.")

    dest = Path(norm_path(cfg["destination"]))
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    snap = dest / "snapshots" / stamp
    snap.mkdir(parents=True, exist_ok=True)

    files = discover(cfg, emit, should_cancel)

    try:
        total = sum(f.stat().st_size for f in files)
        keep = int(cfg.get("keep_snapshots", 30))
        emit(
            f"Payload {total / 1073741824:.2f} GB per snapshot · roughly "
            f"{total * keep / 1073741824:.1f} GB once {keep} snapshots accumulate"
        )
    except Exception:
        pass

    manifest = {
        "run": stamp,
        "started": _dt.datetime.now().isoformat(timespec="seconds"),
        "machine": os.environ.get("COMPUTERNAME", ""),
        "user": os.environ.get("USERNAME", ""),
        "projects": [],
    }
    counts = {HEALTHY: 0, WARNING: 0, CRITICAL: 0, UNREADABLE: 0}
    copied = skipped = 0
    cancelled = False
    mapx = MapxWorker(emit) if cfg.get("export_mapx") else None

    for i, f in enumerate(files, 1):
        if should_cancel and should_cancel():
            cancelled = True
            emit("Cancelled — finishing the file in progress.")
            break
        if progress:
            progress(i, len(files), f"copying · {f.name}")

        try:
            h = inspect_aprx(f)
        except Exception as e:
            # A single unreadable project must never take down the run.
            emit(f"[error] {f.name} — inspection failed: {type(e).__name__}: {e}")
            skipped += 1
            counts[UNREADABLE] = counts.get(UNREADABLE, 0) + 1
            continue

        counts[h.status] = counts.get(h.status, 0) + 1

        tag = {HEALTHY: "OK", WARNING: "WARN", CRITICAL: "BROKEN", UNREADABLE: "LOCKED"}[h.status]
        target = snapshot_target(snap, f, cfg, tag)

        if h.status == UNREADABLE:
            emit(f"[skip] {f.name} — {h.headline}")
            skipped += 1
        else:
            try:
                shutil.copy2(f, target)
                copied += 1
                emit(f"[{tag:>6}] {f.name} — {h.headline}")
            except PermissionError:
                emit(f"[locked] {f.name} — in use, not copied")
                skipped += 1
                h.status = UNREADABLE
            except Exception as e:
                emit(f"[error] {f.name} — {type(e).__name__}: {e}")
                skipped += 1

        if mapx is not None and h.status == HEALTHY and target.exists():
            try:
                n = mapx.export(target, target.parent / f"{f.stem}_mapx")
                if n:
                    emit(f"  exported {n} maps to .mapx")
            except Exception as e:
                emit(f"  mapx export error on {f.name}: {type(e).__name__}: {e}")

        rec = asdict(h)
        rec.pop("details", None)
        rec["backup"] = str(target.relative_to(snap)) if target.exists() else None
        manifest["projects"].append(rec)

    if mapx is not None:
        mapx.stop()

    manifest["finished"] = _dt.datetime.now().isoformat(timespec="seconds")
    manifest["copied"] = copied
    manifest["skipped"] = skipped
    manifest["counts"] = counts
    manifest["cancelled"] = cancelled
    manifest["total_found"] = len(files)
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if cancelled:
        partial = snap.with_name(snap.name + "_PARTIAL")
        try:
            snap.rename(partial)
        except Exception:
            partial = snap
        log_line(cfg, f"run {stamp} CANCELLED after {copied} of {len(files)} files")
        emit(
            f"Stopped after {copied} of {len(files)} projects. Kept as "
            f"{partial.name} — not a complete restore point, and it will not "
            "count toward retention."
        )
        return {
            "stamp": stamp,
            "counts": counts,
            "copied": copied,
            "skipped": skipped,
            "snapshot": str(partial),
            "zip": None,
            "cancelled": True,
        }

    zip_path = None
    zip_cancelled = False
    if cfg.get("make_zip", True):
        arc_dir = dest / "archives"
        arc_dir.mkdir(parents=True, exist_ok=True)
        zip_path = arc_dir / f"{stamp}.zip"

        items = [p for p in snap.rglob("*") if p.is_file()]
        emit(f"Bundling {len(items)} files…")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for j, item in enumerate(items, 1):
                    if should_cancel and should_cancel():
                        zip_cancelled = True
                        break
                    if progress:
                        progress(j, len(items), f"bundling · {item.name}")
                    z.write(item, item.relative_to(snap))
        except Exception as e:
            emit(f"Bundling failed: {type(e).__name__}: {e}")
            zip_cancelled = True

        if zip_cancelled:
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass
            zip_path = None
            emit(
                "Bundling stopped — the snapshot folder itself is complete and "
                "usable, only the .zip was discarded."
            )
        else:
            emit(
                f"Archive written: {zip_path.name} "
                f"({zip_path.stat().st_size / 1048576:.1f} MB)"
            )
            if not cfg.get("keep_loose_copies", True):
                shutil.rmtree(snap, ignore_errors=True)

    prune(cfg, emit)

    append_history(
        cfg,
        {
            "run": stamp,
            "counts": counts,
            "copied": copied,
            "skipped": skipped,
            "projects": {p["name"]: p["status"] for p in manifest["projects"]},
        },
    )
    log_line(
        cfg,
        f"run {stamp}: {copied} copied, {skipped} skipped, "
        f"{counts[HEALTHY]} healthy / {counts[WARNING]} warning / {counts[CRITICAL]} critical",
    )

    summary = {
        "stamp": stamp,
        "counts": counts,
        "copied": copied,
        "skipped": skipped,
        "snapshot": str(snap),
        "zip": str(zip_path) if zip_path else None,
        "cancelled": False,
    }
    emit(
        f"Done. {copied} copied, {skipped} skipped. "
        f"{counts[HEALTHY]} healthy, {counts[WARNING]} warning, {counts[CRITICAL]} critical."
    )
    return summary


# ----------------------------------------------------------------------------
# Theme system
# ----------------------------------------------------------------------------

THEMES = {
    "slate": {
        "label": "Slate",
        "bg": "#1e1f22",
        "panel": "#282a2e",
        "panel_hi": "#31343a",
        "border": "#3a3d42",
        "text": "#e6e6e4",
        "muted": "#93938d",
        "accent": "#7f77dd",
        "ok": "#63a832",
        "warn": "#d99420",
        "err": "#d64a4a",
        "idle": "#6b6f76",
        "caption": "#1e1f22",
        "caption_text": "#e6e6e4",
        "caption_border": "#3a3d42",
        "radius": 10,
        "upper": False,
        "bar": 14,
        "rule": False,
    },
    "console": {
        "label": "Console",
        "bg": "#141619",
        "panel": "#1c1f24",
        "panel_hi": "#242830",
        "border": "#2f353d",
        "text": "#d7dce1",
        "muted": "#79828d",
        "accent": "#3fb2c4",
        "ok": "#4aa96c",
        "warn": "#d99b3c",
        "err": "#d95f5f",
        "idle": "#5a616b",
        "caption": "#0f1113",
        "caption_text": "#3fb2c4",
        "caption_border": "#2f353d",
        "radius": 2,
        "upper": True,
        "bar": 6,
        "rule": True,
    },
}


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------


def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, font as tkfont

    cfg = load_config()
    results = []
    msgq = queue.Queue()
    busy = threading.Event()
    cancel_evt = threading.Event()

    root = tk.Tk()
    root.title(f"{APP_NAME} — ArcGIS Pro project backup")

    # Size relative to the actual screen so the window looks right whether
    # it opens on a 1080p or a 4K monitor, instead of one fixed pixel size
    # that is cramped on one and tiny on the other.
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    win_w = min(max(int(screen_w * 0.65), 1060), 1700)
    win_h = min(max(int(screen_h * 0.70), 760), 1100)
    root.geometry(f"{win_w}x{win_h}")
    root.minsize(900, 580)

    def pick_font(candidates, fallback):
        fams = {f.lower() for f in tkfont.families()}
        for c in candidates:
            if c.lower() in fams:
                return c
        return fallback

    SANS = pick_font(["Segoe UI", "Inter", "Helvetica Neue", "Arial"], "TkDefaultFont")
    MONO = pick_font(
        ["Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Courier New"],
        "TkFixedFont",
    )

    theme_name = cfg.get("theme", "slate")
    if theme_name not in THEMES:
        theme_name = "slate"
    T = dict(THEMES[theme_name])

    STATUS_LABEL = {
        HEALTHY: "Healthy",
        WARNING: "Suspect",
        CRITICAL: "Broken",
        UNREADABLE: "Locked",
    }

    def status_color(s):
        return {HEALTHY: T["ok"], WARNING: T["warn"], CRITICAL: T["err"]}.get(s, T["idle"])

    def cap(text):
        return text.upper() if T["upper"] else text

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # -- layout ---------------------------------------------------------------
    header = ttk.Frame(root, padding=(18, 14, 18, 8))
    header.pack(fill="x")

    title_lbl = ttk.Label(header, text=APP_NAME, style="H1.TLabel")
    title_lbl.pack(side="left")
    subtitle = ttk.Label(header, text="no scan yet", style="Muted.TLabel")
    subtitle.pack(side="left", padx=(12, 0), pady=(6, 0))

    btn_backup = ttk.Button(header, text="Back up all")
    btn_backup.pack(side="right")
    btn_cancel = ttk.Button(header, text="Cancel", state="disabled")
    btn_cancel.pack(side="right", padx=(0, 8))
    btn_scan = ttk.Button(header, text="Scan now")
    btn_scan.pack(side="right", padx=(0, 8))
    btn_open = ttk.Button(header, text="Open backups")
    btn_open.pack(side="right", padx=(0, 8))
    btn_restore = ttk.Button(header, text="Restore…")
    btn_restore.pack(side="right", padx=(0, 8))
    btn_theme = ttk.Button(header, text="Theme", width=7)
    btn_theme.pack(side="right", padx=(0, 8))

    paths = ttk.Frame(root, padding=(18, 4, 18, 4))
    paths.pack(fill="x")
    paths.columnconfigure(1, weight=1)

    src_var = tk.StringVar(value="; ".join(cfg.get("sources", [])))
    dst_var = tk.StringVar(value=cfg.get("destination", ""))

    lbl_projects = ttk.Label(paths, text=cap("Projects"), style="Field.TLabel")
    lbl_projects.grid(row=0, column=0, sticky="w", pady=3)
    ent_src = ttk.Entry(paths, textvariable=src_var, style="Path.TEntry")
    ent_src.grid(row=0, column=1, sticky="ew", padx=8)
    lbl_backups = ttk.Label(paths, text=cap("Backups"), style="Field.TLabel")
    lbl_backups.grid(row=1, column=0, sticky="w", pady=3)
    ent_dst = ttk.Entry(paths, textvariable=dst_var, style="Path.TEntry")
    ent_dst.grid(row=1, column=1, sticky="ew", padx=8)

    def pick_source():
        d = norm_path(filedialog.askdirectory(title="Folder containing .aprx projects"))
        if d:
            cur = [s for s in src_var.get().split("; ") if s.strip()]
            if d not in cur:
                cur.append(d)
            src_var.set("; ".join(cur))
            persist()

    def pick_dest():
        d = norm_path(filedialog.askdirectory(title="Backup destination"))
        if not d:
            return
        dst_var.set(d)
        persist()
        for s in cfg["sources"]:
            if d.lower().startswith(norm_path(s).lower() + os.sep):
                messagebox.showwarning(
                    APP_NAME,
                    "That backup folder sits inside one of your project folders.\n\n"
                    "It will be excluded from scanning so it works, but a backup on "
                    "the same share as the original only protects you from file "
                    "corruption, not from losing the share. Consider a second copy "
                    "on another drive.",
                )
                break

    ttk.Button(paths, text="Add…", width=8, command=pick_source).grid(row=0, column=2)
    ttk.Button(paths, text="Clear", width=8,
               command=lambda: (src_var.set(""), persist())).grid(row=0, column=3, padx=(6, 0))
    ttk.Button(paths, text="Set…", width=8, command=pick_dest).grid(row=1, column=2)

    opts = ttk.Frame(root, padding=(18, 6, 18, 6))
    opts.pack(fill="x")

    v_recursive = tk.BooleanVar(value=cfg.get("recursive", True))
    v_zip = tk.BooleanVar(value=cfg.get("make_zip", True))
    v_loose = tk.BooleanVar(value=cfg.get("keep_loose_copies", True))
    v_mapx = tk.BooleanVar(value=cfg.get("export_mapx", False))
    v_keep = tk.StringVar(value=str(cfg.get("keep_snapshots", 30)))

    ttk.Checkbutton(opts, text="Search subfolders", variable=v_recursive,
                    command=lambda: persist()).pack(side="left")
    ttk.Checkbutton(opts, text="Bundle as .zip", variable=v_zip,
                    command=lambda: persist()).pack(side="left", padx=(14, 0))
    ttk.Checkbutton(opts, text="Keep loose copies", variable=v_loose,
                    command=lambda: persist()).pack(side="left", padx=(14, 0))
    ttk.Checkbutton(opts, text="Export .mapx (arcpy)", variable=v_mapx,
                    command=lambda: persist()).pack(side="left", padx=(14, 0))
    lbl_keep = ttk.Label(opts, text=cap("Keep"), style="Muted.TLabel")
    lbl_keep.pack(side="left", padx=(18, 4))
    keep_entry = ttk.Entry(opts, textvariable=v_keep, width=5, style="Path.TEntry")
    keep_entry.pack(side="left")
    keep_entry.bind("<FocusOut>", lambda e: persist())
    lbl_snaps = ttk.Label(opts, text=cap("snapshots"), style="Muted.TLabel")
    lbl_snaps.pack(side="left", padx=(4, 0))

    def persist():
        cfg["sources"] = [norm_path(s) for s in src_var.get().split("; ") if s.strip()]
        cfg["destination"] = norm_path(dst_var.get())
        src_var.set("; ".join(cfg["sources"]))
        dst_var.set(cfg["destination"])
        cfg["recursive"] = v_recursive.get()
        cfg["make_zip"] = v_zip.get()
        cfg["keep_loose_copies"] = v_loose.get()
        cfg["export_mapx"] = v_mapx.get()
        cfg["theme"] = theme_name
        try:
            cfg["keep_snapshots"] = max(1, int(v_keep.get()))
        except ValueError:
            cfg["keep_snapshots"] = 7
            v_keep.set("7")
        save_config(cfg)

    wrap = tk.Frame(root)
    wrap.pack(fill="both", expand=True, padx=18, pady=(6, 6))

    canvas = tk.Canvas(wrap, highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    # -- footer ---------------------------------------------------------------
    footer = ttk.Frame(root, padding=(18, 4, 18, 14))
    footer.pack(fill="x")

    statebar = ttk.Frame(footer)
    statebar.pack(fill="x", pady=(0, 6))
    state_lbl = ttk.Label(statebar, text="● READY", style="State.TLabel")
    state_lbl.pack(side="left")
    detail_lbl = ttk.Label(statebar, text="", style="Detail.TLabel")
    detail_lbl.pack(side="right")

    prog = ttk.Progressbar(footer, style="Bar.Horizontal.TProgressbar", mode="determinate")
    prog.pack(fill="x")
    status = ttk.Label(footer, text="Set your folders, then press Scan now.",
                       style="Status.TLabel", anchor="w")
    status.pack(fill="x", pady=(8, 0))

    # -- theming --------------------------------------------------------------
    def style_titlebar():
        """Windows 11 (build 22000+) lets an app recolour its own caption via
        DWM. Silently does nothing on Windows 10, macOS or Linux."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes

            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())

            def as_bgr(hexcolor):
                h = hexcolor.lstrip("#")
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return ctypes.c_int((b << 16) | (g << 8) | r)

            # DWMWA_BORDER_COLOR=34, DWMWA_CAPTION_COLOR=35, DWMWA_TEXT_COLOR=36
            for attr, key in ((34, "caption_border"), (35, "caption"), (36, "caption_text")):
                value = T.get(key)
                if not value:
                    continue
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(as_bgr(value)), ctypes.sizeof(ctypes.c_int)
                )
        except Exception:
            pass

    def apply_theme():
        style.configure("TFrame", background=T["bg"])
        style.configure("TLabel", background=T["bg"], foreground=T["text"],
                        font=(SANS, 14))
        style.configure("Muted.TLabel", background=T["bg"], foreground=T["muted"],
                        font=(SANS, 14))
        style.configure("Field.TLabel", background=T["bg"], foreground=T["muted"],
                        font=(SANS, 12 if T["upper"] else 14))
        style.configure("H1.TLabel", background=T["bg"], foreground=T["text"],
                        font=(SANS, 16))
        style.configure("Status.TLabel", background=T["bg"], foreground=T["text"],
                        font=(SANS, 16))
        style.configure("State.TLabel", background=T["bg"], foreground=T["muted"],
                        font=(SANS, 16, "bold"))
        style.configure("Detail.TLabel", background=T["bg"], foreground=T["muted"],
                        font=(MONO, 14))
        style.configure("TButton", font=(SANS, 14), padding=(12, 6),
                        background=T["panel_hi"], foreground=T["text"],
                        borderwidth=0, focuscolor=T["panel_hi"])
        style.map("TButton",
                  background=[("active", T["border"]), ("disabled", T["panel"])],
                  foreground=[("disabled", T["idle"])])
        style.configure("TCheckbutton", background=T["bg"], foreground=T["text"],
                        font=(SANS, 13))
        style.map("TCheckbutton", background=[("active", T["bg"])],
                  foreground=[("active", T["text"])])
        style.configure("Path.TEntry", fieldbackground=T["panel"],
                        foreground=T["text"], borderwidth=0, insertcolor=T["text"])
        style.configure("Bar.Horizontal.TProgressbar", background=T["accent"],
                        troughcolor=T["panel"], borderwidth=0, thickness=T["bar"])
        style.configure("TScrollbar", background=T["panel"], troughcolor=T["bg"],
                        borderwidth=0, arrowcolor=T["muted"])

        root.configure(bg=T["bg"])
        wrap.configure(bg=T["bg"])
        canvas.configure(bg=T["bg"])
        ent_src.configure(font=(MONO, 9))
        ent_dst.configure(font=(MONO, 9))
        lbl_projects.configure(text=cap("Projects"))
        lbl_backups.configure(text=cap("Backups"))
        lbl_keep.configure(text=cap("Keep"))
        lbl_snaps.configure(text=cap("snapshots"))
        style_titlebar()
        draw()

    def cycle_theme():
        nonlocal T, theme_name
        names = list(THEMES.keys())
        theme_name = names[(names.index(theme_name) + 1) % len(names)]
        T = dict(THEMES[theme_name])
        persist()
        apply_theme()
        set_state(current_state[0], current_state[1])

    btn_theme.configure(command=cycle_theme)

    # -- run state ------------------------------------------------------------
    current_state = ["READY", None]

    def set_state(name, color=None):
        current_state[0] = name
        current_state[1] = color
        state_lbl.configure(text=f"● {name}", foreground=color or T["muted"])

    hist_cache = {"runs": []}

    def refresh_history():
        try:
            hist_cache["runs"] = load_history(cfg)[-6:]
        except Exception:
            hist_cache["runs"] = []

    def history_for(name):
        return [h.get("projects", {}).get(name) for h in hist_cache["runs"]]

    hit_zones = []

    def on_wheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind_all("<MouseWheel>", on_wheel)

    def on_click(event):
        x, y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        for x0, y0, x1, y1, fn in hit_zones:
            if x0 <= x <= x1 and y0 <= y <= y1:
                fn()
                return

    canvas.bind("<Button-1>", on_click)

    def panel(x0, y0, x1, y1, fill, outline=""):
        r = T["radius"]
        if r <= 3:
            return canvas.create_rectangle(x0, y0, x1, y1, fill=fill,
                                           outline=outline or T["border"],
                                           width=1 if T["rule"] else 0)
        pts = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return canvas.create_polygon(pts, smooth=True, fill=fill, outline=outline)

    _font_cache = {}

    def _get_font(family, size):
        key = (family, size)
        fnt = _font_cache.get(key)
        if fnt is None:
            fnt = tkfont.Font(root=root, family=family, size=size)
            _font_cache[key] = fnt
        return fnt

    def shorten(text, px, family, size):
        """Trim from the left, keeping the tail, to fit an exact pixel width."""
        fnt = _get_font(family, size)
        if px <= 0 or not text:
            return ""
        if fnt.measure(text) <= px:
            return text
        ellipsis = "…"
        if fnt.measure(ellipsis) > px:
            return ""
        lo, hi, best = 0, len(text), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            tail = text[-mid:] if mid else ""
            if fnt.measure(ellipsis + tail) <= px:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ellipsis + (text[-best:] if best else "")

    def clip(text, px, family, size):
        """Trim from the right, keeping the head, to fit an exact pixel width."""
        fnt = _get_font(family, size)
        if px <= 0 or not text:
            return ""
        if fnt.measure(text) <= px:
            return text
        ellipsis = "…"
        if fnt.measure(ellipsis) > px:
            return ""
        lo, hi, best = 0, len(text), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if fnt.measure(text[:mid] + ellipsis) <= px:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return text[:best] + ellipsis

    def rel_folder(path_str):
        p = Path(path_str).parent
        for s in cfg.get("sources", []):
            try:
                return str(p.relative_to(Path(norm_path(s))))
            except ValueError:
                continue
        return str(p)

    def draw():
        canvas.delete("all")
        hit_zones.clear()
        w = max(canvas.winfo_width(), 760)
        y = 8

        counts = {HEALTHY: 0, WARNING: 0, CRITICAL: 0, UNREADABLE: 0}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1

        tiles = [
            ("Healthy", counts[HEALTHY], T["ok"]),
            ("Suspect", counts[WARNING], T["warn"]),
            ("Broken", counts[CRITICAL], T["err"]),
            ("Locked", counts[UNREADABLE], T["idle"]),
        ]
        tw = (w - 3 * 10) / 4.0
        for i, (label, val, col) in enumerate(tiles):
            x0 = i * (tw + 10)
            panel(x0, y, x0 + tw, y + 74, T["panel"])
            canvas.create_text(x0 + 14, y + 18, text=cap(label), anchor="w",
                               fill=T["muted"], font=(SANS, 14))
            canvas.create_text(x0 + 14, y + 48, text=str(val), anchor="w",
                               fill=col, font=(MONO, 20))
        y += 92

        if not results:
            canvas.create_text(w / 2, y + 60,
                               text="Set your folders above, then press Scan now.",
                               fill=T["muted"], font=(SANS, 16))
            canvas.configure(scrollregion=(0, 0, w, y + 140))
            return

        for r in sorted(results, key=lambda x: (STATUS_ORDER.get(x.status, 9), x.name.lower())):
            h = 86
            col = status_color(r.status)
            panel(0, y, w, y + h, T["panel"])
            canvas.create_rectangle(0, y + 12, 3, y + h - 12, fill=col, outline="")

            # Right-side cluster geometry, computed first (before the text
            # column) and pinned to the real row width, so it always sits
            # against the right edge and the text derives its boundary from
            # it — no separate distance to keep in sync by hand.
            btn_w, btn_gap, edge = 80, 8, 16
            b2x1 = w - edge
            b2x0 = b2x1 - btn_w
            b1x1 = b2x0 - btn_gap
            b1x0 = b1x1 - btn_w

            # Wide enough for the "last 6 runs" caption at its current font
            # size — this used to be sized for an 8pt caption and ran into
            # the Restore button once the caption grew to 12pt.
            hist_w = 90
            hx = b1x0 - 30 - hist_w

            meter_w = 140
            mx = hx - 28 - meter_w

            # Text column ends a fixed gap before the meter starts.
            text_right = mx - 20
            avail = max(120, text_right - 36)

            canvas.create_oval(16, y + 20, 26, y + 30, fill=col, outline="")
            canvas.create_text(36, y + 25, text=clip(r.name, avail, SANS, 16), anchor="w",
                               fill=T["text"], font=(SANS, 16))
            canvas.create_text(36, y + 46, text=clip(r.headline, avail, SANS, 14), anchor="w",
                               fill=col if r.status != HEALTHY else T["muted"],
                               font=(SANS, 14))
            canvas.create_text(36, y + 65,
                               text=shorten(rel_folder(r.path), avail, MONO, 12),
                               anchor="w", fill=T["muted"], font=(MONO, 12))

            ratio = r.integrity_ratio
            canvas.create_rectangle(mx, y + 24, mx + meter_w, y + 30,
                                    fill=T["border"], outline="")
            if ratio > 0:
                canvas.create_rectangle(mx, y + 24, mx + max(3, meter_w * ratio),
                                        y + 30, fill=col, outline="")
            meter_txt = (f"child {r.max_child_id} / {r.node_count} nodes"
                         if r.node_count else "index unreadable")
            canvas.create_text(mx, y + 46, text=meter_txt, anchor="w",
                               fill=T["muted"], font=(MONO, 8))

            # Always six slots so the strip keeps a constant width; empty
            # slots sit on the left until enough history accumulates.
            runs = list(history_for(r.name))[-6:]
            runs = [None] * (6 - len(runs)) + runs
            for i, st in enumerate(runs):
                c = status_color(st) if st else T["border"]
                canvas.create_rectangle(hx + i * 11, y + 22, hx + i * 11 + 7, y + 42,
                                        fill=c, outline="")
            canvas.create_text(hx, y + 54, text=cap("last 6 runs"), anchor="w",
                               fill=T["muted"], font=(SANS, 12))

            by0, by1 = y + 30, y + 56

            panel(b1x0, by0, b1x1, by1, T["panel_hi"], outline=T["border"])
            canvas.create_text((b1x0 + b1x1) / 2, (by0 + by1) / 2, text="Restore",
                               fill=T["text"], font=(SANS, 12))
            hit_zones.append((b1x0, by0, b1x1, by1, lambda rr=r: open_restore(rr)))

            label = "Repair" if r.status == CRITICAL and r.separator_hits else "Details"
            panel(b2x0, by0, b2x1, by1, T["panel_hi"], outline=T["border"])
            canvas.create_text((b2x0 + b2x1) / 2, (by0 + by1) / 2, text=label,
                               fill=T["err"] if label == "Repair" else T["text"],
                               font=(SANS, 12))
            fn = (lambda rr=r: do_repair(rr)) if label == "Repair" else (lambda rr=r: show_details(rr))
            hit_zones.append((b2x0, by0, b2x1, by1, fn))

            y += h + 8

        canvas.configure(scrollregion=(0, 0, w, y + 20))

    canvas.bind("<Configure>", lambda e: draw())

    draw_pending = {"on": False}

    def schedule_draw():
        if draw_pending["on"]:
            return
        draw_pending["on"] = True

        def go():
            draw_pending["on"] = False
            draw()

        root.after(180, go)

    # -- actions --------------------------------------------------------------
    def emit(text):
        msgq.put(("log", text))

    def show_details(r):
        lines = [
            f"File: {r.path}",
            f"Modified: {r.modified}",
            f"Size: {r.size_bytes / 1048576:.2f} MB",
            f"Status: {STATUS_LABEL.get(r.status, r.status)}",
            "",
            r.headline,
        ]
        if r.map_count:
            lines.append(f"Maps: {r.map_count}   Project items: {r.item_count}")
        if r.node_count:
            lines.append(f"Index nodes: {r.node_count}   Highest child id: {r.max_child_id}")
        if r.separator_hits:
            lines.append(f"Thousands-separator values: {r.separator_hits}")
        if r.index_error:
            lines.append(f"Parse error: {r.index_error}")
        for d in r.details:
            lines.append("")
            lines.append(d)
        messagebox.showinfo(r.name, "\n".join(lines))

    def do_repair(r):
        if not messagebox.askyesno(
            "Repair index",
            f"Write a repaired copy of {r.name}?\n\n"
            "The original file is never modified. A new .aprx is created "
            "alongside it.",
        ):
            return
        ok, msg, out = repair_aprx(r.path)
        (messagebox.showinfo if ok else messagebox.showerror)("Repair", msg)
        if ok:
            log_line(cfg, f"repaired {r.path} -> {out}")

    job = {"kind": None, "after_backup": None}

    def scan_worker():
        try:
            msgq.put(("log", "Searching folders…"))
            files = discover(cfg, emit, cancel_evt.is_set)
            msgq.put(("clear", None))
            total = len(files)
            done = 0

            # Inspection is network-latency bound, not CPU bound: each project
            # is two small reads out of a ZIP. Running several in flight cuts
            # wall time roughly in proportion to the pool size.
            workers = min(8, max(1, total)) if total else 1
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(inspect_aprx, f): f for f in files}
                try:
                    for fut in as_completed(futures):
                        if cancel_evt.is_set():
                            for pending in futures:
                                pending.cancel()
                            break
                        try:
                            h = fut.result()
                        except Exception:
                            continue
                        done += 1
                        msgq.put(("progress", (done, total, f"inspecting · {h.name}")))
                        msgq.put(("result", h))
                finally:
                    pass

            busy.clear()
            msgq.put(("scan_done", (done, cancel_evt.is_set())))
        except Exception:
            busy.clear()
            msgq.put(("error", traceback.format_exc()))
        finally:
            busy.clear()

    def backup_worker():
        try:
            summary = run_backup(
                cfg,
                emit=emit,
                progress=lambda i, n, name: msgq.put(("progress", (i, n, name))),
                should_cancel=cancel_evt.is_set,
            )
            busy.clear()
            msgq.put(("backup_done", summary))
        except Exception as e:
            busy.clear()
            msgq.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            busy.clear()

    def set_running(running, kind=None):
        state = "disabled" if running else "normal"
        btn_scan.configure(state=state)
        btn_backup.configure(state=state)
        btn_theme.configure(state=state)
        btn_restore.configure(state=state)
        btn_cancel.configure(state="normal" if running else "disabled")
        if running:
            set_state("SCANNING" if kind == "scan" else "BACKUP RUNNING", T["accent"])
        else:
            set_state("READY")

    def do_cancel():
        cancel_evt.set()
        btn_cancel.configure(state="disabled")
        set_state("CANCELLING", T["warn"])
        status.configure(text="Stopping after the current file…")

    def start(worker, silent_scan=False):
        persist()
        if busy.is_set():
            return
        if not cfg["sources"]:
            messagebox.showwarning(APP_NAME, "Add at least one project folder first.")
            return
        if worker is backup_worker and not cfg["destination"]:
            messagebox.showwarning(APP_NAME, "Set a backup destination first.")
            return
        job["kind"] = "backup" if worker is backup_worker else "scan"
        if not silent_scan:
            job["after_backup"] = None
        busy.set()
        cancel_evt.clear()
        prog["value"] = 0
        refresh_history()
        set_running(True, job["kind"])
        msgq.put(("searching", None))
        threading.Thread(target=worker, daemon=True).start()

    def open_dest():
        d = norm_path(dst_var.get())
        if not d:
            messagebox.showwarning(APP_NAME, "Set a backup folder first.")
            return
        if not Path(d).exists():
            if not messagebox.askyesno(
                APP_NAME, f"{d}\n\nThat folder does not exist yet. Create it?"
            ):
                return
            try:
                Path(d).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Could not create it:\n{e}")
                return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", d])
            elif sys.platform == "darwin":
                subprocess.run(["open", d], check=False)
            else:
                subprocess.run(["xdg-open", d], check=False)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def versions_of(target):
        """Every snapshot copy of one project, newest first. Matches on the
        original path so same-named projects in different folders stay
        separate; falls back to name if paths have since changed."""
        by_path, by_name = [], []
        for label, sdir, man in list_snapshots(cfg):
            for rec in man.get("projects", []):
                if not rec.get("backup"):
                    continue
                if rec.get("path") == target.path:
                    by_path.append((label, sdir, rec))
                    break
                if rec.get("name") == target.name:
                    by_name.append((label, sdir, rec))
                    break
        return by_path or by_name

    def open_restore(only=None):
        persist()
        snaps = list_snapshots(cfg)
        if not snaps:
            messagebox.showinfo(
                APP_NAME,
                "No snapshots found yet. Run a backup first, or check the "
                "backup folder setting.",
            )
            return

        entries = []
        if only is not None:
            entries = versions_of(only)
            if not entries:
                messagebox.showinfo(
                    APP_NAME,
                    f"No snapshot contains {only.name} yet.\n\nIt will be "
                    "included from the next backup onwards.",
                )
                return

        win = tk.Toplevel(root)
        win.title(f"Restore — {only.name}" if only is not None else "Restore from snapshot")
        win.configure(bg=T["bg"])
        win.geometry("720x460" if only is None else "640x420")
        win.transient(root)
        win.grab_set()

        head = ttk.Frame(win, padding=(16, 14, 16, 6))
        head.pack(fill="x")

        rows = []
        snap_var = tk.StringVar(value=snaps[0][0])

        if only is not None:
            ttk.Label(head, text=only.name, style="H1.TLabel").pack(anchor="w")
            ttk.Label(head, text=cap("available versions, newest first"),
                      style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        else:
            ttk.Label(head, text=cap("Snapshot"), style="Field.TLabel").pack(side="left")
            combo = ttk.Combobox(head, textvariable=snap_var, state="readonly",
                                 values=[x[0] for x in snaps], width=44)
            combo.pack(side="left", padx=10)

        mid = ttk.Frame(win, padding=(16, 6, 16, 6))
        mid.pack(fill="both", expand=True)

        listbox = tk.Listbox(
            mid, bg=T["panel"], fg=T["text"], selectbackground=T["accent"],
            selectforeground=T["bg"], highlightthickness=0, bd=0,
            font=(MONO, 10), activestyle="none",
        )
        lsb = ttk.Scrollbar(mid, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        listbox.pack(side="left", fill="both", expand=True)

        TAGS = {HEALTHY: "OK     ", WARNING: "SUSPECT", CRITICAL: "BROKEN ",
                UNREADABLE: "LOCKED "}

        def load_versions():
            listbox.delete(0, "end")
            rows.clear()
            for label, sdir, rec in entries:
                tag = TAGS.get(rec.get("status"), "?      ")
                mb = (rec.get("size_bytes") or 0) / 1048576
                listbox.insert("end", f"{label:<28}  [{tag}]  {mb:6.1f} MB")
                rows.append((sdir, rec))
            if rows:
                listbox.selection_set(0)

        def load_snapshot(*_):
            listbox.delete(0, "end")
            rows.clear()
            entry = next((x for x in snaps if x[0] == snap_var.get()), None)
            if not entry:
                return
            _, sdir, man = entry
            for rec in sorted(man.get("projects", []), key=lambda r: r["name"].lower()):
                if not rec.get("backup"):
                    continue
                tag = TAGS.get(rec.get("status"), "?      ")
                listbox.insert("end", f"[{tag}]  {rec['name']}")
                rows.append((sdir, rec))
            if rows:
                listbox.selection_set(0)

        if only is not None:
            load_versions()
        else:
            combo.bind("<<ComboboxSelected>>", load_snapshot)
            load_snapshot()

        ttk.Label(
            win,
            text="Restoring beside the original never overwrites anything.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=16)

        bar = ttk.Frame(win, padding=(16, 8, 16, 14))
        bar.pack(fill="x")

        def selected():
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("Restore", "Pick a version first.", parent=win)
                return None
            return rows[sel[0]]

        def do_beside():
            item = selected()
            if not item:
                return
            sdir, rec = item
            ok, msg, out = restore_project(sdir, rec, replace=False)
            (messagebox.showinfo if ok else messagebox.showerror)("Restore", msg, parent=win)
            if ok:
                log_line(cfg, f"restored beside original: {out}")

        def do_replace():
            item = selected()
            if not item:
                return
            sdir, rec = item
            if not messagebox.askyesno(
                "Replace original",
                f"Overwrite the working copy of {rec['name']}?\n\n"
                "The current file will first be copied to "
                f"{rec['name']}_before_restore_<timestamp>.aprx so you can undo "
                "this.\n\nMake sure the project is closed in ArcGIS Pro.",
                parent=win,
            ):
                return
            ok, msg, out = restore_project(sdir, rec, replace=True)
            (messagebox.showinfo if ok else messagebox.showerror)("Restore", msg, parent=win)
            if ok:
                log_line(cfg, f"replaced original from snapshot: {out}")

        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Replace original…", command=do_replace).pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="Restore beside original",
                   command=do_beside).pack(side="right", padx=(0, 8))

    btn_scan.configure(command=lambda: start(scan_worker))
    btn_backup.configure(command=lambda: start(backup_worker))
    btn_cancel.configure(command=do_cancel)
    btn_open.configure(command=open_dest)
    btn_restore.configure(command=lambda: open_restore(None))

    def stop_marquee():
        if str(prog["mode"]) == "indeterminate":
            prog.stop()
            prog.configure(mode="determinate")

    def pump():
        try:
            while True:
                kind, payload = msgq.get_nowait()

                if kind == "clear":
                    results.clear()

                elif kind == "result":
                    results.append(payload)
                    schedule_draw()

                elif kind == "progress":
                    i, n, name = payload
                    stop_marquee()
                    prog["maximum"] = max(n, 1)
                    prog["value"] = i
                    detail_lbl.configure(text=f"{i} / {n}")
                    status.configure(text=name)

                elif kind == "searching":
                    prog.configure(mode="indeterminate")
                    prog["value"] = 0
                    prog.start(14)
                    detail_lbl.configure(text="")
                    status.configure(
                        text="Searching folders — this can take a minute on a share…"
                    )

                elif kind == "log":
                    status.configure(text=str(payload)[:160])

                elif kind == "scan_done":
                    count, was_cancelled = payload
                    stop_marquee()
                    prog["value"] = 0
                    detail_lbl.configure(text="")
                    set_running(False)
                    subtitle.configure(
                        text=f"{count} projects · {_dt.datetime.now():%H:%M}"
                    )

                    tally = {HEALTHY: 0, WARNING: 0, CRITICAL: 0, UNREADABLE: 0}
                    for r in results:
                        tally[r.status] = tally.get(r.status, 0) + 1
                    breakdown = (
                        f"{tally[HEALTHY]} healthy · {tally[WARNING]} suspect · "
                        f"{tally[CRITICAL]} broken · {tally[UNREADABLE]} locked"
                    )

                    pending = job.get("after_backup")
                    if pending:
                        # This scan was the automatic refresh after a backup.
                        # Report the backup, not the scan.
                        job["after_backup"] = None
                        set_state("BACKUP COMPLETE", T["ok"])
                        status.configure(
                            text=f"Backup {pending['stamp']} — {pending['copied']} projects "
                                 f"copied, {pending['skipped']} skipped. {breakdown}."
                        )
                    elif was_cancelled:
                        set_state("SCAN CANCELLED", T["warn"])
                        status.configure(
                            text=f"Scan stopped after {count} projects. {breakdown}."
                        )
                    else:
                        set_state("SCAN COMPLETE", T["ok"])
                        status.configure(
                            text=f"Scan finished — {count} projects inspected. {breakdown}."
                        )
                    draw()

                elif kind == "backup_done":
                    s = payload
                    stop_marquee()
                    detail_lbl.configure(text="")
                    set_running(False)
                    if s.get("cancelled"):
                        prog["value"] = 0
                        set_state("BACKUP CANCELLED", T["warn"])
                        status.configure(
                            text=f"Backup stopped — {s['copied']} projects copied, kept "
                                 "as a partial run and excluded from retention."
                        )
                    else:
                        prog["value"] = prog["maximum"]
                        set_state("BACKUP COMPLETE", T["ok"])
                        status.configure(
                            text=f"Backup {s['stamp']} — {s['copied']} projects copied, "
                                 f"{s['skipped']} skipped. Refreshing dashboard…"
                        )
                        job["after_backup"] = s
                        start(scan_worker, silent_scan=True)

                elif kind == "error":
                    stop_marquee()
                    prog["value"] = 0
                    set_running(False)
                    set_state("ERROR", T["err"])
                    status.configure(text="Something went wrong — see the dialog.")
                    messagebox.showerror(APP_NAME, str(payload))

        except queue.Empty:
            pass
        root.after(120, pump)

    apply_theme()
    set_state("READY")
    root.after(200, pump)
    if cfg.get("sources"):
        root.after(400, lambda: start(scan_worker))
    root.mainloop()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION}")
    ap.add_argument("--mapx-worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--run", action="store_true", help="run a backup headless and exit")
    ap.add_argument("--check", metavar="FOLDER", help="integrity check only, no copying")
    ap.add_argument("--repair", metavar="APRX", help="write a repaired copy of a broken .aprx")
    ap.add_argument("--source", action="append", help="override source folder (repeatable)")
    ap.add_argument("--dest", help="override destination folder")
    args = ap.parse_args()

    if args.mapx_worker:
        return mapx_worker_main()

    if args.repair:
        ok, msg, out = repair_aprx(args.repair)
        print(msg)
        return 0 if ok else 2

    if args.check:
        worst = 0
        for f in sorted(Path(args.check).rglob("*.aprx")):
            h = inspect_aprx(f)
            print(f"{h.status:<10} {h.name:<44} {h.headline}")
            worst = max(worst, {HEALTHY: 0, UNREADABLE: 1, WARNING: 1, CRITICAL: 2}[h.status])
        return worst

    if args.run:
        cfg = load_config()
        if args.source:
            cfg["sources"] = args.source
        if args.dest:
            cfg["destination"] = args.dest
        try:
            s = run_backup(cfg, emit=print)
        except Exception as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 2
        return 1 if (s["counts"][CRITICAL] or s["counts"][WARNING]) else 0

    launch_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
