---
name: wegstr-pcb-slicer
description: Drive the Wegstr PCB Slicer MCP server (server name "wegstr") to turn Gerber archives or individual Gerber/Excellon files into machine-ready G-code for a Wegstr Light CNC, and to extend the app itself. Use when the task involves PCB milling, isolation routing, tool selection from a Vectric tool database, or the Wegstr PCB Slicer application.
---

# Wegstr PCB Slicer

A desktop PCB slicer for the **Wegstr Light CNC** (140 x 200 x 40 mm,
`G21`/`G90`, `M3`/`M5` spindle). The MCP server exposes the whole application.

## Connect

The server is registered with Claude Code as `wegstr`. If it is not connected,
run:

```
claude mcp add wegstr -s user -- "<path>\Wegstr PCB Slicer.exe" --mcp
```

or add it to `.mcp.json`:

```json
{
  "mcpServers": {
    "wegstr": {
      "command": "C:\\path\\to\\Wegstr PCB Slicer.exe",
      "args": ["--mcp"]
    }
  }
}
```

## Standard workflow

| Step | Tool | What to check |
|---|---|---|
| 1 | `app_info` | version, tool database, whether a board is loaded |
| 2 | `list_tools` | available cutters, grouped, with Wegstr feeds |
| 3 | `open_archive` or `add_files` | load a `.zip` or individual files |
| 4 | `list_layers` | every file's assigned role; fix with `roles` if wrong |
| 5 | `recommend_tools` | measures copper clearance, picks cutters |
| 6 | `plan` | operation list, runtime, warnings |
| 7 | `export` | numbered `.gcode` files + generated `README.md` |

## Loading files one at a time

`add_files` takes a `paths` array and an optional `roles` map. Roles:
`edge_cuts`, `top_copper`, `bottom_copper`, `top_silk`, `bottom_silk`,
`top_mask`, `bottom_mask`, `drill_pth`, `drill_npth`.

```json
{
  "paths": ["board.GTL", "board.GBL", "board.GKO", "Drill_PTH.DRL"],
  "roles": { "board.GTL": "top_copper" }
}
```

Roles are guessed from the file name and content when not given. Always confirm
with `list_layers` before planning.

## Hard rules

- A board larger than 140 x 200 mm cannot be cut. `list_layers` reports
  `fits_work_area`; `plan` fails hard on oversize boards.
- Isolation depth is normally 0.05 mm. Cut-out depth must exceed board
  thickness (1.6 mm default).
- Read `plan.warnings` aloud to the user. Flip instructions for double-sided
  boards live there.
- Never claim a job is ready without running `plan` and reading its warnings.

## Configuration

`get_config` returns the live settings. `set_config` patches them:

- tools: `isolation_tool`, `rubout_tool`, `cutout_tool`, `silkscreen_tool`
- isolation: `isolation_depth`, `isolation_passes`, `isolation_stepover`
- rub-out: `rubout_enabled`, `rubout_depth`
- cut-out: `cutout_depth`, `cutout_stepdown`, `tab_count`, `tab_width`
- sides: `mill_top`, `mill_bottom`, `mirror_bottom`
- silkscreen: `silkscreen_enabled`, `silkscreen_mode`, `silkscreen_depth`
- alignment: `alignment_holes`, `alignment_hole_depth`, `flip_axis`
- drilling: `drill_enabled`, `board_thickness`, `peck_depth`, `drill_first`

## Extending the app

The app was designed to be extended at runtime.

1. `describe_module` with a module name to see classes, functions, signatures.
   Modules: `gerber_io`, `pcb_engine`, `wegstr_gcode`, `exporter`,
   `gcode_verify`, `gcode_reader`, `tool_db`, `sqlite_read`, `updater`,
   `sample_gerber`, `main`.
2. `python` runs arbitrary code with those modules imported and the live session
   bound to `state` (`state.project`, `state.config`, `state.profile`,
   `state.plan`, `state.tool_db`).
3. Persist a real change with `write_file`, then re-run `plan` to confirm.

`python` is unrestricted: it can read and write any file on the machine.

## Resources

- `wegstr://app`, `wegstr://project`, `wegstr://config`, `wegstr://plan`,
  `wegstr://tools`
- `wegstr://skill` - this document
- `wegstr://source/<module>` - a module's full source

## Prompts

- `wegstr-build-board` - mill a board end to end
- `wegstr-extend-app` - add a feature that does not exist yet
- `wegstr-inspect` - summarise the live session and flag machine risks
