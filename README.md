# Dead Reckoning

**Backup and index-integrity analyzer for ArcGIS Pro projects.**

This tool detects `Index.json` corruption that can make every map and table
in an `.aprx` fail to open, keeps dated snapshots tagged with each project's 
health at the time of backup, and repairs broken projects without touching 
the original file.

No third-party packages. Python 3.9+. Windows. `arcpy` is optional
and only needed for the `.mapx` export option.

---

## How to know if this will help you

If you have experienced the following:

- Maps are listed in the Catalog pane but **none of them will open**
- No error dialog, no crash, no exception — the project just doesn't work
- Diagnostic Monitor shows the map lookup completing in **0 ms** with no failure
- The project opened fine yesterday and you changed nothing structural
- Opening the `.aprx` as a ZIP shows `Index.json` containing values like
  `"NumberOfNodes" : 1,376`

---

## Contents

1. [The failure this catches](#1-the-failure-this-catches)
2. [Install and first run](#2-install-and-first-run)
3. [Reading the dashboard](#3-reading-the-dashboard)
4. [Running a backup](#4-running-a-backup)
5. [What a backup produces](#5-what-a-backup-produces)
6. [Restoring](#6-restoring)
7. [Repairing a broken project](#7-repairing-a-broken-project)
8. [Retention](#8-retention)
9. [Themes and customization](#9-themes-and-customization)
10. [Command line and scheduling](#10-command-line-and-scheduling)
11. [The .mapx option](#11-the-mapx-option)
12. [What gets scanned, and what doesn't](#12-what-gets-scanned-and-what-doesnt)
13. [Performance notes](#13-performance-notes)
14. [Troubleshooting](#14-troubleshooting)
15. [Habits worth keeping](#15-habits-worth-keeping)
16. [Support and scope](#16-support-and-scope)
17. [Contributing](#17-contributing)
18. [License](#18-license)

---

## 1. The failure this catches

An `.aprx` is a ZIP archive containing CIM documents plus `Index.json`, a
lookup table mapping internal `CIMPATH=` references to entries in the archive.

If `Index.json` is written with culture-aware number formatting, values above
999 get thousands separators — `"NumberOfNodes" : 1,376` — which is not valid
JSON. ArcGIS Pro then cannot parse its own index. The failure is silent: maps
still appear in the Catalog pane but none of them will open, and Diagnostic
Monitor shows the lookup completing in 0 ms with no exception raised.

Two things follow from that:

- The corruption happens **at save time**, and you find out the next time you
  open the project. A single rolling backup would overwrite your last good copy
  before you knew anything was wrong.
- The defect is **detectable in seconds** by parsing `Index.json` outside of
  Pro, which is what this tool does on every scan.

Utility network rules — connectivity, containment, attribute rules, subnetwork
definitions — live in the enterprise geodatabase, not the `.aprx`, and are not
at risk from this failure. What the `.aprx` holds is maps, layers, symbology,
popups, labeling, layouts, and stored table views.

### What the tool does

1. Scans one or more folders for `.aprx` files
2. Opens each as a ZIP and validates `Index.json` without launching Pro
3. Copies each project into a timestamped snapshot, tagging the filename
   `OK`, `WARN`, or `BROKEN`
4. Bundles the snapshot into a single `.zip`
5. Prunes old snapshots according to a retention setting
6. Optionally exports every map to `.mapx` (needs `arcpy`)
7. Repairs a corrupt `.aprx` by writing a new, corrected file

---

## 2. Install and first run

There is no installer. Download the repository and put these three files
somewhere permanent, for example `C:\Tools\DeadReckoning\`:

```
dead_reckoning.py
Dead Reckoning.bat
README.md
```

Edit the `.bat` if your Pro install is not in the default location, then
double-click it.

In the window:

1. **PROJECTS** — press `Add…` and pick the folder holding your `.aprx` files.
   Subfolders are searched by default.
2. **BACKUPS** — press `Set…` and pick where snapshots go.
3. Press **Scan now**.

Settings save automatically to `%APPDATA%\DeadReckoning\config.json`.

**On choosing a destination.** If you put the backup folder inside a project
folder, the tool warns you once and then excludes it from scanning so it still
works. But a backup on the same share as the original only protects you from
file corruption, not from losing the share. Consider periodically copying a zip
to a different drive.

**On choosing a source.** Point it at the folders holding live projects rather
than at a broad root. A narrower source means faster scans and a dashboard you
can actually read.

---

## 3. Reading the dashboard

### Status

| Status | Meaning | Action |
|---|---|---|
| **Healthy** | `Index.json` parses, child ids consistent | None |
| **Suspect** | Parses, but something might be bad — see below | Usually none; see the two cases |
| **Broken** | `Index.json` will not parse | Press **Repair**, then Save As in Pro |
| **Locked** | File in use, almost always open in Pro | Close Pro and rescan |

Suspect covers two different situations:

- *No stored index.* Normal for an unpacked package or a project saved with no
  maps open. Not the corruption signature, and nothing to fix.
- *Child id ceiling truncated.* Node count above 999 but no child id above 999.
  This is the residual form of the corruption, where four-digit ids were split
  on commas. Open in Pro and **Save As** to rebuild the index.

### The integrity meter

The bar is highest child id divided by node count. Near full is normal — a
clean index typically shows the highest child one below the node count, e.g.
`child 906 / 907 nodes`. A short bar on a project with more than 999 nodes is
the signature of comma-split ids.

### Last 6 runs

Six small bars, oldest left, newest right, colored by that project's status on
each of the last six backup runs. Empty slots on the left will fill as you do runs.
Five greens then an amber tells you *when* something changed, which
can indicate which snapshot to restore from.

### Row buttons

- **Restore** — versions of this one project across snapshots. See §6.
- **Details** — full findings: node counts, separator hits, parse error
  location, map and item counts.
- **Repair** — replaces Details, in red, when a project is genuinely broken.

### Status area

Bottom left shows the current state — `● READY`, `● SCANNING`,
`● BACKUP RUNNING`, `● BACKUP COMPLETE`, `● CANCELLING` — with a monospace
counter on the right and a full-sentence result below.

Completion messages always name the operation. A backup ends with
`Backup 2026-08-04_0815 — 3 projects copied, 0 skipped…`, never a bare
"complete", so the automatic dashboard refresh that follows a backup can't be
mistaken for the backup's own result.

---

## 4. Running a backup

Press **Back up all**. The run has two phases, each with its own progress bar:

1. `copying · <n>` — inspects and copies each project
2. `bundling · <n>` — packs the snapshot into a zip

Before copying starts, the status line reports the estimated payload, e.g.
`Payload 1.42 GB per snapshot · roughly 7.1 GB once 5 snapshots accumulate`.
Worth watching on the first run.

**Cancel** stops after the current file, so you never get a half-written
`.aprx` in a snapshot. A cancelled run is renamed with a `_PARTIAL` suffix,
gets no zip, no history entry, and is excluded from the retention count so it
can never push out a good snapshot. Files already copied are kept. Partial runs
are capped at the two most recent.

Cancel works during the folder walk and during bundling too. If bundling is
interrupted, the incomplete zip is deleted but the snapshot folder is kept —
every file was already copied and verified at that point.

Projects open in ArcGIS Pro are reported as **Locked** and skipped rather than
copied mid-write.

---

## 5. What a backup produces

```
<destination>\
    snapshots\
        2026-08-04_0815\
            files\
                <mirrors your source folder structure>\
                    ElectricUtilityNetworkFoundation__OK.aprx
                    SomeOtherProject__WARN.aprx
            manifest.json
        2026-08-03_2137_PARTIAL\        (a cancelled run)
    archives\
        2026-08-04_0815.zip
    _deadreckoning\
        history.json
        activity.log
```

**The `OK` / `WARN` / `BROKEN` suffix is the point.** When you need to restore
you don't have to guess which snapshot predates the corruption.

**Folder structure is mirrored, not flattened.** Projects sharing a filename
are common — three copies of a tracker in different folders — and flattening
would silently overwrite two of them. Paths are stored relative to whichever
source root you configured, so the same project can appear at different depths
in snapshots taken with different source settings. That's cosmetic; version
matching keys off the original absolute path recorded in `manifest.json`.

**Modification times are preserved.** A restored file shows the date the
project was genuinely last edited, not the date it was copied. The
`_restored_<stamp>` suffix is what tells you the backup date.

**`snapshots\` versus `archives\`.** The snapshot folders are the working
restore points and are what the Restore button reads. The zips are the portable
copy — drag one off to long-term storage. If you uncheck **Keep loose copies**,
snapshot folders are deleted after bundling and Restore will report no
snapshots found. Leave it checked unless you have a reason not to.

---

## 6. Restoring

Two ways in:

**Per project — the usual case.** Press **Restore** on a row. You get that one
project's versions across snapshots, newest first:

```
2026-08-04_0815              [OK     ]     9.5 MB
2026-07-28_0810              [OK     ]     9.4 MB
2026-07-21_0812              [SUSPECT]     9.4 MB
```

**By snapshot.** The header **Restore…** button browses a whole snapshot. Use
this when a project has been deleted or moved and no longer appears in the scan.

Then choose one of two actions:

| Action | What happens |
|---|---|
| **Restore beside original** | Writes `<n>_restored_<snapshot>.aprx` next to the original. Nothing is overwritten. |
| **Replace original…** | Overwrites the working file, but first copies it to `<n>_before_restore_<timestamp>.aprx`. |

Restore beside is the default and the recommended path: open the restored copy,
confirm the maps load, then swap it in yourself. Replace exists for when you're
confident, and even then the safety copy is your undo.

Close the project in ArcGIS Pro before replacing.

Every restore is logged to `_deadreckoning\activity.log` with the full source
path.

---

## 7. Repairing a broken project

Press **Repair** on any Broken row, or:

```
python dead_reckoning.py --repair "C:\Projects\YourProject.aprx"
```

It strips thousands separators from numeric values in `Index.json`, confirms
the result parses, and writes `<n>_repaired_<timestamp>.aprx` next to the
original. **The source file is never modified.**

Then open the repaired file, confirm all maps load, and immediately **Save As**
to a new project so Pro regenerates `Index.json` from its in-memory model.

The scripted repair fixes the numeric literals but cannot recover the
`ChildNodeIds` strings, because a comma inside a delimited id list is
indistinguishable from a delimiter. Only Pro can rebuild those. **The tool will
keep flagging the repaired file as Suspect until you do that rebuild.** That is
correct behavior, not a false positive.

---

## 8. Retention

The **KEEP** box sets how many snapshots to retain. Default for a fresh config
is 7.

Retention applies at the end of every run: it lists snapshot folders and
archive zips, sorts by date, and deletes everything older than the newest N. So
lowering KEEP from 30 to 5 means the next run deletes 25 of them. No separate
cleanup step, and disk usage plateaus instead of growing.

Pruning is date-ordered only. It does not preserve the oldest known-good
snapshot. **For a long-term archive, move a zip out of `archives\` into a folder
of your own** — anything outside the destination tree is never touched.

Sizing: with weekly runs, 5 snapshots is roughly five weeks of history. In the
environment this was written for, the defect surfaced three times in four
years, so that window comfortably covers "I broke it and noticed the next
morning" while a manually-kept archive copy covers the rare case.

---

## 9. Themes and customization

Two themes, cycled by the **Theme** button and persisted to config:

- **Slate** — rounded panels, purple accent, mixed case labels
- **Console** — graphite background, teal accent, sharp 2px panels with hairline
  borders, uppercase section labels, thin progress bar

Everything visual lives in the `THEMES` dict near the top of the file:

```python
"console": {
    "bg": "#141619",              # window background
    "panel": "#1c1f24",           # card background
    "panel_hi": "#242830",        # buttons
    "border": "#2f353d",          # hairlines, empty meter track
    "text": "#d7dce1",
    "muted": "#79828d",
    "accent": "#3fb2c4",          # progress bar, running state
    "ok": "#4aa96c",              # Healthy
    "warn": "#d99b3c",            # Suspect
    "err": "#d95f5f",             # Broken
    "idle": "#5a616b",            # Locked
    "caption": "#0f1113",         # title bar background
    "caption_text": "#3fb2c4",    # title bar text
    "caption_border": "#2f353d",  # window border
    "radius": 2,                  # corner radius; <= 3 draws sharp rectangles
    "upper": True,                # uppercase section labels
    "bar": 6,                     # progress bar thickness in px
    "rule": True,                 # outline panels
},
```

Change a hex value, save, relaunch. Add a third theme by copying a block and
giving it a new key — the Theme button cycles through whatever is in the dict.

The three `caption` keys use the Windows 11 DWM API and need build 22000 or
later. On Windows 10, macOS, or Linux they silently do nothing and you get the
standard title bar.

Fonts are detected at runtime: Cascadia Mono, else Consolas, else a generic
fixed font for paths, filenames, counters and figures; Segoe UI or a fallback
for prose and buttons. Numbers are monospaced so digits keep their column and
the layout doesn't twitch as counts change during a scan.

Row layout is drawn in `draw()` on a plain tkinter Canvas. Column positions are
computed right-to-left from the window edge with fixed gutters, so adding a
column means adjusting that one block.

---

## 10. Command line and scheduling

```
python dead_reckoning.py                       # GUI
python dead_reckoning.py --run                 # headless backup, uses saved config
python dead_reckoning.py --check "C:\Projects" # integrity check only, no copying
python dead_reckoning.py --repair "<file>"     # write a repaired copy
python dead_reckoning.py --run --source "D:\A" --dest "E:\Backups"
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | All projects healthy |
| 1 | At least one warning or critical finding |
| 2 | The run itself failed |

**Task Scheduler**

1. Task Scheduler → Create Task (not Basic Task).
2. General → Run whether user is logged on or not.
3. Triggers → New → Weekly, at a time when Pro is closed.
4. Actions → New → Start a program.
   - Program: `C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`
   - Arguments: `"C:\Tools\DeadReckoning\dead_reckoning.py" --run`
   - Start in: `C:\Tools\DeadReckoning`
5. Settings → uncheck "Stop the task if it runs longer than" if `.mapx` export
   is on.

Configure once in the GUI first; headless mode reads the same config file.

Any Python 3.9+ works. Pro's bundled interpreter is only required for `.mapx`
export.

---

## 11. The .mapx option

Off by default; slow and needs a license checkout. When enabled, every map in
each healthy project is exported to `.mapx` beside its backup copy.

A `.mapx` carries layers, grouping, draw order, symbology, labeling and popup
configuration, and does not depend on a project index resolving — so it survives
this entire failure class. Worth enabling on an occasional run even if you leave
it off normally.

The export runs against the backup copy, never your working file.

---

## 12. What gets scanned, and what doesn't

Skipped by default:

- Folders matching `~$`, `backup`, `_deadreckoning`, `recover`,
  `extractpackage`, `unpacked`, `scratch`, `temp`, `appdata`
- The backup destination itself, including when it sits inside a source folder
- GUID-named `.aprx` files such as
  `1DF1B976-ABF0-43AD-951D-085E677492BC.aprx`, which are package extractions
  rather than real projects

Exclusions are plain substring matches against folder and file names, editable
under `exclude_patterns` in `%APPDATA%\DeadReckoning\config.json`.

The destination subtree is pruned during the walk rather than filtered
afterwards. Without that, every run would walk every previous snapshot, getting
slower each week.

---

## 13. Performance notes

The bottleneck is network latency, not CPU.

**Inspection runs 8 in parallel.** Each project is two small reads out of a ZIP
and the machine is idle waiting on the share, so running several in flight cuts
wall time roughly in proportion to the pool size. Change `max_workers` in
`scan_worker()` if you want a different number.

**Bundling uses `ZIP_STORED`, not deflate.** An `.aprx` is already a
deflate-compressed ZIP, so recompressing it burns time for essentially zero size
reduction.

**The folder walk can't have a percentage** — the file count isn't known until
the walk finishes. It shows a marquee plus live counts
(`Searching — 450 folders, 96 projects so far…`) instead.

**Copying is deliberately serial.** Progress is visible and per-file, and
failures are attributable to a specific file. Parallel copying would gain less
than parallel inspection anyway, since SMB throughput caps it.

---

## 14. Troubleshooting

**A dialog is open and no buttons respond.** tkinter dialogs are modal. Dismiss
it and the window comes back.

**"The system cannot find the file specified" on a UNC path.** Should be fixed —
paths are normalized on entry — but if you hand-edit `config.json`, use
backslashes for UNC paths. `//server/share` is invalid to some Windows APIs.

**Scan finds far more projects than expected.** The source root is too broad,
or something is slipping past the exclusions. Check what the extra rows are and
add a pattern to `exclude_patterns`.

**Restore says no snapshots found.** Either no backup has run, the destination
is set wrong, or **Keep loose copies** is unchecked and only zips remain.

**Every project shows "index unreadable" on the meter but reads Healthy.** The
child-id check looks for the keys `NumberOfNodes` and `ChildNodeIds`. If a
future Pro version renames those, the check goes quietly inert rather than
failing loudly. Node counts of zero across the board is the tell.

**The app is unresponsive during a long run.** Scans and backups run on a
worker thread and the window should stay live. If it doesn't, that's a bug
worth reporting.

---

## 15. Habits worth keeping

1. **Close COGO Reader before saving.** Both observed incidents had it open at
   save. The corruption lands in the node index, which is what tracks open panes
   and table views. Same goes for attribute tables.
2. **Turn on Pro's own backup interval** — Options → Application → General.
3. **Do a restore drill occasionally.** An untested backup is a hypothesis. Ten
   minutes now beats finding out during an incident.
4. **Keep a copy of any broken project you encounter.** A genuinely corrupt
   `.aprx` paired with a known-good one is the best validation set there is:
   point `--check` at both and confirm the broken one reports its separator hits
   and parse failure while the good one reads Healthy. If you are willing to
   share a sanitized example, see [Contributing](#17-contributing).

---

## 16. Support and scope

This is provided as-is, with no warranty, under the MIT License. It was written
to solve a specific problem in one production environment and is shared because
that problem is poorly documented elsewhere.

**Tested against:** ArcGIS Pro 3.x on Windows 10 and 11, projects on both local
disk and SMB shares.

**Not tested against:** ArcGIS Pro 2.x, macOS, Linux. The GUI is tkinter and
will render on other platforms, but the Windows 11 title bar theming, the
default Pro interpreter paths, and `%APPDATA%` handling are Windows-oriented.

**Read §7 before repairing anything.** The repair writes a new file and never
modifies the original, but the result still needs a Save As in Pro to be fully
correct.

Issues and questions are welcome, but this is maintained by one person alongside
a full-time job. Responses may be slow or may not come at all. If it breaks for
you and you fix it, a pull request is more likely to land than an issue is to
get a fix written for you.

---

## 17. Contributing

The most useful contributions, roughly in order:

1. **Sample files.** A corrupt `.aprx` and a matching good one, with any
   sensitive layers or data source paths removed, is worth more than a bug
   report. Detection logic is only as good as its test cases.
2. **Reports of the failure in the wild** — Pro version, what was open at save
   time, whether COGO Reader or an attribute table was involved. The root cause
   is understood; the trigger conditions are not fully mapped.
3. **Key-name changes across Pro versions.** If `NumberOfNodes` or
   `ChildNodeIds` ever get renamed, the integrity check goes inert. Knowing
   which version changed them would be valuable.
4. **Bug fixes and platform fixes**, as pull requests.



---

## 18. License

MIT. See [LICENSE](LICENSE).
