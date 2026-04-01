# /pcb-design

Full autonomous PCB design loop. You are the architect — scripts are your CAD team. You handle net classification, placement strategy, corridor planning, routing order, and trade-off decisions. Scripts handle scoring, pathfinding, DRC, and spatial analysis.

**Usage:** `/pcb-design $ARGUMENTS`

Where `$ARGUMENTS` is the path to a `.kicad_pcb` file.

---

## PHASE 1 — ANALYZE

```bash
python -m src.kicad_export "$ARGUMENTS" -o /tmp/board.json
python -m src.board_analyzer /tmp/board.json --text
python -m src.placement_scorer /tmp/board.json
python -m src.conflict_analyzer /tmp/board.json
```

Read ALL output carefully. Note:
- Board dimensions and component positions
- Which gaps are passable and at what trace width
- Which components have center gaps (SMD ICs)
- Net difficulty scores and routing order
- Channel bottlenecks
- Composite placement score

Store the baseline score. Classify every net mentally: ground (skip), power (route first, wide traces in open corridors), constrained signal (dedicated corridor), signal (whatever's left).

---

## PHASE 2 — PLACEMENT OPTIMIZATION (up to 4 rounds)

Repeat until score gain < 1 point or 4 rounds reached.

For each round, identify the worst sub-score and propose moves:
- **Crossings high** → move components to untangle ratsnest
- **Channel oversubscribed** → increase spacing between bottleneck pair
- **Wirelength high** → move connected components closer
- **Gaps too narrow for routing** → spread components to open trace channels

Apply moves directly:
```python
python3 -c "
from src.schema import load_board, save_board, snap_to_grid
board = load_board('/tmp/board.json')
board.components['C2'].position = (snap_to_grid(21.0, 0.3), snap_to_grid(24.6, 0.3))
save_board(board, '/tmp/board.json')
"
```

Re-run `board_analyzer` and `placement_scorer` after each move. Keep if improved, revert if worse.

---

## PHASE 3 — CORRIDOR ASSIGNMENT

Based on board_analyzer output, assign each hard net to a spatial corridor:
- Power nets: identify a trunk line through the widest corridor connecting most pads
- Long signals: find a clear path that avoids the power trunk
- Short connections: will be autorouted, don't plan

Write your corridor assignments as comments before routing. Example:
```
+3.3V: vertical trunk at x≈22.5 on F.Cu from y=25 to y=45, branches left to R1/J4
AVDD: through U1 center gap, up between C1/C2 at x≈21.6, north to J1
GPIO12: F.Cu from R1 south to y=38, then diagonal to J2.7
```

---

## PHASE 4 — STRATEGIC ROUTING (hard nets, one at a time)

Route the 2-3 hardest nets using the trace tool. Work one net at a time.

```bash
# Clear any existing routes for this net first
python3 -c "
from src.schema import load_board, save_board
from src.trace_tool import remove_net_routes
board = load_board('/tmp/board.json')
board = remove_net_routes(board, '+3.3V')
save_board(board, '/tmp/board.json')
"

# Draw the route
python -m src.trace_tool /tmp/board.json --plan '{
  "net": "+3.3V",
  "waypoints": [[22.8,25.6],[22.5,28.1],[22.5,34.3],[22.5,42.0],[26.4,45.0]],
  "layer": "F.Cu",
  "width_mm": 0.5
}' -o /tmp/board.json
```

After each net, export and run KiCad DRC:
```bash
python -m src.kicad_import /tmp/board.json --base "$ARGUMENTS" -o /tmp/routed.kicad_pcb
python -m src.kicad_drc /tmp/routed.kicad_pcb
```

### ESCALATION LADDER

After each DRC failure, classify it and respond at the right level:

**LEVEL 1 — ROUTING FIX (max 2 attempts)**
- Try different waypoints or a different corridor
- Try narrower trace (0.3mm instead of 0.5mm) — check board_analyzer gaps to know what fits
- Try a via to switch layers for crossing another trace
- If 2 attempts fail → escalate

**LEVEL 2 — PLACEMENT FIX**
- Check board_analyzer gaps: if the gap is < trace_width + 0.4mm, the fix is moving a component
- Common moves: spread caps apart, shift IC slightly, move connector along its edge
- Apply the move, re-run board_analyzer to verify the gap opened
- Re-attempt routing
- If the move creates new problems → revert, escalate

**LEVEL 3 — EXPLAIN TO HUMAN**
- Present what you tried (routes AND placement moves)
- Identify the specific obstacle with coordinates
- Offer options with trade-offs:
  - "Move C2 east by 0.5mm to open a 1.2mm corridor (currently 0.7mm)"
  - "Use a via pair and route on B.Cu (will break ground pour at this location)"
  - "Use 0.3mm trace instead of 0.5mm (thinner but fits the gap)"

---

## PHASE 5 — AUTOROUTE REMAINING NETS

```bash
python -m src.pathfinder /tmp/board.json --skip-nets "+3.3V,Net-(U1-AVDD),Net-(J2-GPIO12)" -o /tmp/board.json
```

The A* handles easy connections: decoupling caps to IC, short signal nets, etc.

---

## PHASE 6 — VALIDATE + ITERATE (up to 3 passes)

```bash
python -m src.kicad_import /tmp/board.json --base "$ARGUMENTS" -o /tmp/routed.kicad_pcb
python -m src.kicad_drc /tmp/routed.kicad_pcb
```

For each violation, apply the escalation ladder. For autorouter failures:
- Clear the offending net and retry with `--via-cost 1`
- If still failing, route it manually with trace_tool

For ground pour islands:
```bash
python -m src.drc_checker /tmp/board.json
```
If `pour_island` errors: reduce B.Cu usage by re-routing offending nets on F.Cu.

---

## PHASE 7 — EXPORT + REPORT

```bash
python -m src.kicad_import /tmp/board.json --base "$ARGUMENTS" -o /tmp/routed.kicad_pcb
python -m src.visualizer /tmp/board.json -o /tmp/board.svg --show-ratsnest
```

Present final report:

```
## PCB Design Complete

**Input:** $ARGUMENTS
**Output:** /tmp/routed.kicad_pcb

### Placement
- Baseline score: {baseline} → Final: {final} (+{delta})
- Changes: {list of component moves with reasoning}

### Routing Strategy
- Corridor assignments: {which net uses which path}
- Agent-routed: {nets with waypoints}
- Autorouted: {remaining nets}

### Results
- Nets routed: {n}/{total}
- Segments: {n}, Vias: {n}
- KiCad DRC: {violations} violations, {unconnected} unconnected

### Issues Requiring Human Action
{For each remaining issue — specific, actionable:}
- Net X: gap between C2 and U1 is 0.7mm, need 0.9mm for 0.5mm trace
  → Option A: move C2 east 0.3mm
  → Option B: use 0.3mm trace (fits current gap)
  → Option C: route on B.Cu via pair (breaks pour at Y)

### Files
- Board JSON: /tmp/board.json
- KiCad PCB: /tmp/routed.kicad_pcb
- SVG: /tmp/board.svg
```

---

## EE Reasoning Prompts

Apply these throughout all phases:

- **Pad positions reveal the natural trunk.** Look at where pads cluster — the trunk runs through the densest cluster.
- **Bypass caps go tight to power pins.** Don't route power around them — route through them or between them.
- **Prefer F.Cu for SMD pads.** B.Cu is only for crossing other traces. Every B.Cu segment breaks the ground pour.
- **Use 0.3mm traces in tight gaps.** Only use 0.5mm for power trunks in open corridors. Check board_analyzer gap widths.
- **After 2 routing failures: STOP tweaking coordinates.** Ask whether a component move would help. Check the gap width — if it's < trace + 0.4mm, the fix is placement.
- **Clearance violation < 0.3mm = placement problem.** The trace path is fundamentally too narrow. Move something.
- **When you fail, explain WHY.** Not "failed to route" — "the gap between C2 and U1 pin 4 is 0.95mm but I need 0.9mm for a 0.5mm trace with clearance. Moving C2 east by 0.3mm would open a 1.25mm corridor."
