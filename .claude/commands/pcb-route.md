# /pcb-route

Route an already-placed board. Assumes placement is finalized (board.json exists with good placement score). Runs conflict analysis, routes all nets, validates with DRC, and iterates to fix problems.

**Usage:** `/pcb-route $ARGUMENTS`

Where `$ARGUMENTS` is a `board.json` path (already exported and placed).

---

## Instructions

### PHASE 1 — PRE-ROUTING ANALYSIS

```bash
python -m src.conflict_analyzer "$ARGUMENTS"
python -m src.placement_scorer "$ARGUMENTS"
```

Read the conflict report:
- Note routing order recommendation
- Identify oversubscribed channels and plan for via usage there
- Identify nets likely to need multiple routing attempts

---

### PHASE 2 — ROUTE

Route all nets following the recommended order:
```bash
python -m src.pathfinder "$ARGUMENTS" -o /tmp/routed.json
```

Check output: how many nets routed, how many failed?

**If nets failed:**
1. Try routing them individually with lower via cost (easier layer changes):
   ```bash
   python -m src.pathfinder /tmp/routed.json --net NETNAME --via-cost 2 -o /tmp/routed.json
   ```
2. Check if the net has pads in oversubscribed channels — if so, note that placement adjustment may be needed
3. Try routing the net on a specific layer by using via cost 999 (forces single-layer):
   ```bash
   python -m src.pathfinder /tmp/routed.json --net NETNAME --via-cost 999 -o /tmp/routed.json
   ```

---

### PHASE 3 — VALIDATE AND FIX (up to 3 passes)

```bash
python -m src.drc_checker /tmp/routed.json
```

For each error:

- `unrouted`: net still not connected
  - Clear and re-route: re-run pathfinder with just that net and `--via-cost 2`
  - If still failing after retry, it needs manual placement work (flag it)

- `short`: routing collision between nets
  - Identify which nets collide and at what location
  - Remove the conflicting net's route from board.json (edit routes list)
  - Re-route the conflicting net with higher via-cost to force a different path

Repeat until no errors or 3 passes done.

---

### PHASE 4 — VISUALIZE + EXPORT

```bash
python -m src.visualizer /tmp/routed.json -o /tmp/routed.svg --show-ratsnest
```

Find the original `.kicad_pcb` to import back into (if provided, otherwise note that kicad_import needs to be run manually).

If a base `.kicad_pcb` is available:
```bash
python -m src.kicad_import /tmp/routed.json --base ORIGINAL.kicad_pcb -o /tmp/routed.kicad_pcb
```

---

### FINAL REPORT

```
## Routing Complete

**Input:** $ARGUMENTS
**Output:** /tmp/routed.json

### Routing Results
- Nets routed: {n_routed}/{n_total}
- Segments: {n_segs}
- Vias: {n_vias}
- Failed nets: {list or "none"}

### DRC
- Errors: {n_errors}
- Warnings: {n_warnings}

### Issues Requiring Manual Action
{For each remaining error: specific location, what needs to move, which KiCad tool to use}

### Files
- Routed JSON: /tmp/routed.json
- Visualization: /tmp/routed.svg
```
