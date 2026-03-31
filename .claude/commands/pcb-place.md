# /pcb-place

Placement-only optimization loop. Exports the board, scores placement, iteratively improves it, and exports a placed (unrouted) board for manual routing in KiCad.

Use this when you want full control over routing, or when trying different placements before committing to routing.

**Usage:** `/pcb-place $ARGUMENTS`

Where `$ARGUMENTS` is the path to a `.kicad_pcb` file.

---

## Instructions

### PHASE 1 — BASELINE

```bash
python -m src.kicad_export "$ARGUMENTS" -o /tmp/board.json
python -m src.placement_scorer /tmp/board.json
python -m src.conflict_analyzer /tmp/board.json
```

Read and record: composite score, sub-scores, oversubscribed channels, routing difficulty by net.

---

### PHASE 2 — ITERATIVE PLACEMENT (up to 4 rounds)

Repeat until gain < 1 point or 4 rounds reached.

For each round, reason about which sub-score is worst and what specific moves would fix it:

- High crossings → rearrange components to untangle MST edges
- Oversubscribed channels → increase gap between bottleneck pairs, or rotate to reduce net count through gap
- Pin escape violations → move affected components away from obstacles
- High wirelength → move connected components closer

Generate moves.json and apply:
```bash
python -m src.placement_sweeper /tmp/board.json --moves moves.json --top 1 -o /tmp/board.json
python -m src.placement_scorer /tmp/board.json
```

Keep changes only if score improves. Revert and try a different approach if not.

---

### PHASE 3 — VISUALIZE + EXPORT

```bash
python -m src.visualizer /tmp/board.json -o /tmp/board.svg --show-ratsnest --show-corridors
python -m src.kicad_import /tmp/board.json --base "$ARGUMENTS" -o /tmp/placed.kicad_pcb
```

Note: `kicad_import` with no routes will just produce a clean file with updated component positions. This file can be opened in KiCad for manual routing.

---

### FINAL REPORT

```
## Placement Optimization Complete

**Input:** $ARGUMENTS
**Output:** /tmp/placed.kicad_pcb (unrouted — ready for KiCad routing)

### Score
- Baseline: {baseline}
- Final: {final} (+{delta})

### Changes Made
{Round-by-round summary of what was moved/rotated and why}

### Routing Guidance (for manual routing in KiCad)
Based on conflict analysis, route in this order:
1. {net} — {reason: power/high-difficulty}
2. ...

Bottleneck channels:
- {comp_a}↔{comp_b}: {required} nets need to cross, {available} tracks available
  → Suggestion: {specific advice}

### Files
- board.json: /tmp/board.json
- Ratsnest SVG: /tmp/board.svg
- KiCad PCB: /tmp/placed.kicad_pcb
```
