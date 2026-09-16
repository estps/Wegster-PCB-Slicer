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
python backend/test_pipeline.py                    # run the test suite
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
  pcb_engine.py     CAM geometry: isolation, rub-out, tabs, drilling, depth
  wegstr_gcode.py   Wegstr Light kinematics profile + G-code emitter
  exporter.py       multi-file export and the generated build guide
  gcode_verify.py   independent verifier (re-reads the emitted file)
  gcode_reader.py   independent parser for the viewer
  main.py           CLI + JSON IPC server

app/              PySide6 desktop UI
  window.py         main window, threading, presets
  viewer.py         pixmap-cached toolpath canvas
  theme.py          dark design system
  settings.py       persistent preferences
```

The verifier and the reader parse independently of the emitter on purpose — a
bug in one can't hide a bug in the other.

## Why not `pcb-tools`?

`pcb-tools` (the `gerber` package) is unmaintained and dies on Python 3.11+
because it still opens files in the `'rU'` mode that was removed. `pygerber`
doesn't expose raw shapely geometry, which is what the CAM math needs. So the
Gerber and Excellon parsers here are self-contained.

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
