# /pcb-constrain

Conversational placement constraint elicitation. Has a focused conversation with the user about their board's mechanical and electrical requirements, then writes a `constraints.json` file and applies it to `board.json`.

**Usage:** `/pcb-constrain $ARGUMENTS`

Where `$ARGUMENTS` is the path to a `.kicad_pcb` file OR an already-exported `board.json`.

---

## Instructions

You are helping the user define hard placement rules for their PCB. These constraints will be machine-enforced — constrained components will never be moved by the optimizer.

### STEP 1 — Load and display the board

If given a `.kicad_pcb` file:
```bash
python -m src.kicad_export "$ARGUMENTS" -o /tmp/board.json
```

If given a `.json` file, use it directly as `/tmp/board.json`.

Then list every component so the user knows what references are available:
```bash
python -c "
import json
board = json.load(open('/tmp/board.json'))
for ref, comp in sorted(board['components'].items()):
    fp = comp['footprint']
    pos = comp['position']
    rot = comp['rotation']
    print(f'  {ref:<8} {fp:<35} pos=({pos[0]:.1f},{pos[1]:.1f}) rot={rot}°')
"
```

Also show board dimensions:
```bash
python -c "
import json
board = json.load(open('/tmp/board.json'))
xs = [p[0] for p in board['board_outline']]
ys = [p[1] for p in board['board_outline']]
print(f'Board: {max(xs)-min(xs):.1f} × {max(ys)-min(ys):.1f} mm')
print(f'Edges: left={min(xs):.1f}  right={max(xs):.1f}  top={min(ys):.1f}  bottom={max(ys):.1f}')
"
```

Output this as a clean summary to the user.

---

### STEP 2 — Ask about placement requirements

Ask the user one focused question at a time, working through these areas:

**A. Edge-mounted / facing-out connectors**
> "Which components need to be mounted at a specific board edge? For each one, tell me: which edge (left / right / top / bottom), and how far the center should be from the edge in mm (0 = flush with edge, or give a specific inset like 3mm)."

For each edge-mounted component, follow up:
> "What rotation should [REF] have? (0° = right-facing, 90° = up-facing, 180° = left-facing, 270° = down-facing in KiCad's CCW convention)"

**B. Aligned component groups**
> "Are there components that need to be aligned with each other — for example, a row of headers that must share the same X or Y coordinate?"

For each group, follow up:
> "Should they align on the X axis (vertically stacked, same X position) or Y axis (in a row, same Y position)? And do they have a required center-to-center spacing in mm?"

**C. Fixed-position components**
> "Are there any components whose position is fixed by mechanical constraints — mounting holes, specific pads that must hit a connector footprint on the mating board, etc.?"

**D. Free-form notes**
> "Any other placement requirements I should know about? (e.g., 'C1 must be within 5mm of U1', 'no components in the top-left 10×10mm area')"

Note: zone-based constraints and proximity constraints are noted as `notes` on the component but not yet machine-enforced — flag this clearly.

---

### STEP 3 — Confirm with the user

Before writing any files, show a plain-English summary of what you're about to encode:

```
Here's what I'll enforce:

J1 (RJ45):     → right edge, inset 0mm, rotation 0° (facing right/outward), locked
J2 (Header):   → bottom edge, inset 3mm, rotation 270°, aligned with J3 on X axis
J3 (Header):   → bottom edge, inset 3mm, rotation 270°, aligned with J2 on X axis, 7.62mm spacing
U1 (MCU):      free — optimizer will place it

Does this look right? Any corrections?
```

Wait for the user to confirm or correct before writing files.

---

### STEP 4 — Write constraints.json and apply

Write `/tmp/constraints.json` in this format:
```json
{
  "J1": {
    "constraint": "edge",
    "edge": "right",
    "allowed_rotations": [0],
    "offset_from_edge_mm": 0.0,
    "notes": "RJ45 facing outward on right edge"
  },
  "J2": {
    "constraint": "edge",
    "edge": "bottom",
    "allowed_rotations": [270],
    "offset_from_edge_mm": 3.0,
    "align_group": "bottom_headers",
    "align_axis": "x",
    "spacing_mm": 7.62,
    "notes": "Debug header, bottom edge"
  },
  "J3": {
    "constraint": "edge",
    "edge": "bottom",
    "allowed_rotations": [270],
    "offset_from_edge_mm": 3.0,
    "align_group": "bottom_headers",
    "align_axis": "x",
    "notes": "Power header, bottom edge, aligned with J2"
  }
}
```

Then apply to board.json:
```bash
python -m src.apply_constraints /tmp/board.json /tmp/constraints.json -o /tmp/board.json
```

---

### STEP 5 — Verify and report

```bash
python -m src.apply_constraints /tmp/board.json /tmp/constraints.json --check-only
python -m src.placement_scorer /tmp/board.json
```

Show the user:
1. List of constrained components with their applied positions/rotations
2. Any constraint violations (should be zero after apply)
3. Placement score — note that constrained components are now locked and the optimizer will only move free components

Final message:
```
Constraints applied. constraints.json saved to /tmp/constraints.json.

To run placement optimization respecting these constraints:
  /pcb-optimize $ARGUMENTS

To adjust constraints later, just run /pcb-constrain again — it will re-read the current board and let you update anything.
```

---

## Field reference for constraints.json

| Field | Values | Effect |
|-------|--------|--------|
| `constraint` | `"free"` `"edge"` `"fixed"` | free=optimizer can move; edge=snapped to board edge; fixed=never moved |
| `edge` | `"left"` `"right"` `"top"` `"bottom"` | which board edge to snap to |
| `allowed_rotations` | `[0]` `[90]` `[0,180]` etc. | list of legal rotations; single value locks rotation |
| `offset_from_edge_mm` | `0.0` `3.0` etc. | center distance from edge (0=flush) |
| `align_group` | any string | all components sharing this name are aligned together |
| `align_axis` | `"x"` `"y"` | `"x"` = same X coord (vertically stacked); `"y"` = same Y coord (horizontal row) |
| `spacing_mm` | `7.62` `2.54` etc. | center-to-center distance between group members |
| `notes` | any string | human-readable, shown in reports |
