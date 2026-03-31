# /pcb-optimize

Full autonomous PCB optimization loop. Analyzes placement, improves it iteratively, routes all nets, validates with DRC, and keeps iterating until the board is genuinely good — not just a one-shot attempt.

**Usage:** `/pcb-optimize $ARGUMENTS`

Where `$ARGUMENTS` is the path to a `.kicad_pcb` file.

---

## What this skill does

Runs the complete optimization loop end-to-end, analyzing outputs at each stage and deciding what to fix. Presents a single final report when the result is complete.

---

## Instructions

You are the PCB design reasoning engine. Work through these phases, reading each output carefully and deciding what to change before proceeding.

### PHASE 1 — BASELINE

```bash
python -m src.kicad_export "$ARGUMENTS" -o /tmp/board.json
python -m src.placement_scorer /tmp/board.json
python -m src.conflict_analyzer /tmp/board.json
```

Read the output. Note:
- Composite score and all sub-scores (crossings, wirelength, channel capacity, pin escape)
- Which nets have difficulty > 1.0 (will be hardest to route)
- Which channels are oversubscribed
- Which pads have escape violations

Store the baseline composite score.

---

### PHASE 2 — PLACEMENT OPTIMIZATION (up to 4 rounds)

Repeat until composite score gain < 1 point or 4 rounds reached.

**Decision logic — pick ONE focus per round based on worst sub-score:**

- `ratsnest_crossings` high → move the components most involved in crossing edges closer together and rearrange so their connections don't cross. Check which nets cross and which components those nets connect.
- `channel_capacity` oversubscribed → increase spacing between the bottleneck component pairs, or rotate components so fewer nets need to cross the gap.
- `pin_escape_violations` > 0 → move violating components away from board edges or other components.
- `total_wirelength_mm` high → move components connected by long MST edges closer together.

**For each round:**
1. Reason about what to change and why (write your reasoning as a comment).
2. Generate a `moves.json` with specific position/rotation changes:
   ```json
   [
     {"reference": "C1", "position": [x, y], "rotation": 0},
     {"reference": "U1", "position": [x, y], "rotation": 90}
   ]
   ```
3. Apply and score:
   ```bash
   python -m src.placement_sweeper /tmp/board.json --moves moves.json --top 1 -o /tmp/board.json
   python -m src.placement_scorer /tmp/board.json
   ```
4. If the score improved: keep the change, note the delta.
5. If the score got worse: revert — re-export from the original `.kicad_pcb` and replay only the rounds that improved.
6. Re-run conflict analysis to get updated routing order.

---

### PHASE 3 — ROUTE

```bash
python -m src.pathfinder /tmp/board.json -o /tmp/board.json
```

Read the output. Note:
- How many segments and vias were placed
- Which nets (if any) failed to route

**If nets failed:**
- Analyze why: check if they are in oversubscribed channels, have blocked pad escape, or are simply long
- Try routing them individually with higher via cost:
  ```bash
  python -m src.pathfinder /tmp/board.json --net NETNAME --via-cost 3 -o /tmp/board.json
  ```
- If still failing: note this for the final report — it likely requires manual placement adjustment

---

### PHASE 4 — VALIDATE (iterative DRC loop, up to 3 passes)

```bash
python -m src.drc_checker /tmp/board.json
python -m src.visualizer /tmp/board.json -o /tmp/board.svg --show-ratsnest
```

**Read DRC output carefully.**

For each error type, decide what to fix:

- `unrouted` errors → the pathfinder missed connections. Try re-routing the specific net:
  ```bash
  python -m src.pathfinder /tmp/board.json --net NETNAME -o /tmp/board.json
  ```
  If still unrouted after retry, it requires placement adjustment. Flag it.

- `short` errors → two nets share a cell. This is a routing bug. Clear and re-route the conflicting nets:
  - Re-export from original `.kicad_pcb` and re-route without those nets first to reserve space, then add them
  - Or manually note the conflict location for the final report

- `edge_clearance` warnings → traces near board edge. Usually acceptable but note them.

- `trace_width` errors → should not occur with correct pathfinder; flag as implementation bug if seen.

Repeat DRC → fix loop until either:
- No errors remain, or
- 3 passes done and errors persist (flag for manual fix)

---

### PHASE 5 — EXPORT + FINAL REPORT

```bash
python -m src.kicad_import /tmp/board.json --base "$ARGUMENTS" -o /tmp/routed.kicad_pcb
```

Present a single final report:

```
## PCB Optimization Complete

**Input:** $ARGUMENTS
**Output:** /tmp/routed.kicad_pcb

### Placement
- Baseline score: {baseline}
- Final score: {final} (+{delta} improvement)
- Rounds run: {n}
- Key changes made: {list of what was moved/rotated and why}

### Routing
- Nets routed: {n_routed}/{n_total}
- Segments: {n_segs}, Vias: {n_vias}
- Failed nets: {list or "none"}

### DRC
- Errors: {n_errors}
- Warnings: {n_warnings}

### What's left for you to do in KiCad
{Only if there are remaining issues:}
- [ERROR] Net 'X' unrouted — the gap between U1 and J1 is too narrow; try moving them 2mm apart
- [ERROR] Short between 'A' and 'B' at (x,y) on F.Cu — re-route manually
- [WARN] 3 traces near left edge — verify clearance in KiCad DRC

### Files
- Board JSON: /tmp/board.json
- Visualization: /tmp/board.svg
- KiCad PCB: /tmp/routed.kicad_pcb
```

If there are DRC errors, give specific, actionable guidance for manual KiCad fixes. Do not just say "fix the errors" — say exactly what to move, where, and why.
