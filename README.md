# Wegstr PCB Slicer

A desktop PCB slicer and G-code generator for the **Wegstr Light CNC**.
Feed it a Gerber archive, get back machine-ready G-code — plus a step-by-step
build guide telling you which file to run, with which tool, and when to flip
the board.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## What it does

- **Gerber + Excellon parsing** — reads `.zip` archives from KiCad, EasyEDA,
  Altium and friends. Auto-detects front/back copper, board outline, silkscreen,
  solder mask and drill files.
- **Tool database** — reads your Vectric Cut2D `.vtdb` file, lists every tool,
  and **auto-selects** the isolation, rub-out, cut-out and silkscreen tools by
  measuring the board's real copper clearance. Every dropdown stays editable, so
  you can override any choice by hand. The Wegstr feed, plunge and spindle
  figures stored against each tool are used per-operation.
- **Isolation milling** — single or multi-pass offset paths, with V-bit tip
  compensation so the cut width tracks the plunge depth.
- **Rub-out / copper clearing** — optional removal of the unetched copper pour.
- **Silkscreen cut-out** — apply the silkscreen by hand, then mill it back off
  the pad openings so the pads still take solder.
- **Board cut-out with break-away tabs** — tabs are nudged onto straight runs so
  they snap cleanly.
- **Peck drilling** — one file per bit, full retracts to clear chips.
- **Double-sided registration** — two deep pin holes define the flip axis (the
  perpendicular bisector of the pair), so the back layer registers without
  re-zeroing X or Y.
- **G-code verification** — every generated file is re-read and checked against
  the machine envelope before you ever hit cycle start.
- **G-code viewer** — open any `.gcode`, scrub through it, and see rapids,
  plunges and tool changes with playback.
- **Update check** — on launch it asks GitHub whether a newer release exists and
  offers a download link. It never installs anything by itself.
- **Finds itself in Windows** — on first launch it adds a Start-menu shortcut
  (per-user, no admin needed) so you can type "Wegstr" into the search bar and
  hit it. Done once; remove it with `python tools\install_start_menu.py --remove`.

## Requirements

- Python 3.11+
- `numpy`, `shapely`, `PySide6`

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Running

```bash
.venv\Scripts\python run.py
```

Then: **Open Gerber…** → pick your `.zip` → check the preview → **Export Files…**

The tool database is found automatically in `Documents\Cut2D_tools_database.vtdb`.
Point at a different one by setting `WEGSTR_TOOL_DB`:

```bash
set WEGSTR_TOOL_DB=D:\CAM\my_tools.vtdb
```

To add it to the Start menu (per-user, no admin needed):

```bash
.venv\Scripts\python tools\install_start_menu.py
```

## Command line

The backend also works headless:

```bash
python backend/main.py --info                      # layer report
python backend/main.py --out board --passes 3      # write G-code
python backend/main.py --out board --bottom --silkscreen --alignment-holes
python backend/main.py --list-tools                # dump the tool database
python backend/main.py --use-tool-db --out board   # auto-select tools, then cut
python backend/test_pipeline.py                    # run the test suite
python run.py --self-test                          # end-to-end check of a build
```

## Output

A job is split into one file per operation, numbered in execution order:

```
01_alignment.gcode                 2 deep pin holes, no tool change
02_drill.gcode                     every hole, one bit at a time
03_isolation_top.gcode             front copper
04_silkscreen_cutout_top.gcode     clears the applied silkscreen off the pads
05_isolation_bottom.gcode          back copper  (flip the board first)
06_silkscreen_cutout_bottom.gcode
07_board_cutout.gcode              last, so the part stays attached
README.md                          generated build guide
```

The generated `README.md` lists the tools, the times, the setup checklist, and
what to do at every step — including exactly how to flip the board.

## Architecture

```
backend/          pure Python, no UI dependency
  gerber_io.py      RS-274X + Excellon parser (aperture macros, arcs,
                    regions, polarity, step-and-repeat)
  sqlite_read.py    minimal read-only SQLite file reader (no sqlite3.dll)
  pcb_engine.py     CAM geometry: isolation, rub-out, tabs, drilling, depth
  tool_db.py        Vectric .vtdb reader, clearance measurement, tool choice
  wegstr_gcode.py   Wegstr Light kinematics profile + G-code emitter
  exporter.py       multi-file export and the generated build guide
  gcode_verify.py   independent verifier (re-reads the emitted file)
  gcode_reader.py   independent parser for the viewer
  updater.py        GitHub release check
  main.py           CLI + JSON IPC server

app/              PySide6 desktop UI
  window.py         main window, threading, presets
  viewer.py         pixmap-cached toolpath canvas
  shortcuts.py      first-run Start-menu registration
  theme.py          dark design system
  settings.py       persistent preferences
```

The verifier and the reader parse independently of the emitter on purpose — a
bug in one can't hide a bug in the other.

## Tool selection

The tool database holds the real Wegstr tools with their cutting data (170 mm/min
feed, 11000 rpm, and a step-down per tool). On load the slicer measures the
smallest distance between separate copper nets — the clearance the isolation
cutter has to fit through — and picks the widest tool that fits, preferring a
flat end mill over a V-bit for speed. Rub-out gets the largest bit that still
clears the traces; cut-out and silkscreen fall back to sensible 1.0 mm and 0.3 mm
end mills. If no database is found it uses built-in defaults, and the dropdowns
always let you choose something else.

## Why not `pcb-tools`?

`pcb-tools` (the `gerber` package) is unmaintained and dies on Python 3.11+
because it still opens files in the `'rU'` mode that was removed. `pygerber`
doesn't expose raw shapely geometry, which is what the CAM math needs. So the
Gerber and Excellon parsers here are self-contained. The SQLite reader for the
tool database is self-contained too, so the only runtime dependencies stay
`numpy`, `shapely` and `PySide6`.

The SQLite reader is not an accident either: bundling `sqlite3.dll` makes
Windows **Smart App Control** refuse to launch the built `.exe`, so the `.vtdb`
file is parsed directly instead.

The build also sets `noarchive=True`, which stores the Python modules beside the
executable instead of inside it. The `.exe` becomes a ~350 KB bootloader that
Windows is happy to run, and `--self-test` gives you a way to confirm a built
copy actually parses a board and plans toolpaths before you trust it.

## Safety

This generates real G-code for a real machine. Before running anything:

- Check the toolpath preview against the board.
- Read the **G-code Verification** panel — it re-reads the emitted file and
  reports envelope violations, rapids with the tool down, cuts with the spindle
  off, and missing program ends.
- Dry-run above the work surface first.

The software is provided as-is. You are responsible for what you cut.

## License

MIT — see [LICENSE](LICENSE).
