# Research: Agent-Driven Routing Strategy

## Relevant Architecture

The project is an AI-assisted PCB routing system where Claude acts as the design reasoning engine and Python scripts handle deterministic computation. The pipeline:

```
.kicad_pcb → [kicad_export] → board.json → [tools] → board.json → [kicad_import] → .kicad_pcb
```

All communication between tools uses `board.json`. The current A* grid router (`pathfinder.py`) handles the full routing pipeline autonomously, but produces suboptimal results because it has no strategic understanding of the board — it just finds shortest paths through an occupancy grid.

The user's insight: **Claude (Opus) should be the routing strategist**, planning where major routes go (power trunks, signal corridors), while a simpler point-to-point trace tool draws the actual segments. The A* becomes a fallback for tight spaces, not the primary routing engine.

## Key Files

### 1. Route/Segment/Via Data Model — `src/schema.py`

The format any new trace tool must produce:

```python
@dataclass
class Segment:
    start: tuple[float, float]  # board coordinates (mm)
    end: tuple[float, float]
    layer: str                  # "F.Cu" or "B.Cu"

@dataclass
class Route:
    net: str           # net name, e.g. "+3.3V"
    width_mm: float    # trace width
    segments: list[Segment]

@dataclass
class Via:
    position: tuple[float, float]
    net: str
    drill_mm: float
```

**board.json example** of a route (from current routing):
```json
{
  "net": "+3.3V",
  "width_mm": 0.5,
  "segments": [
    {"start": [22.5, 36.6], "end": [22.5, 36.3], "layer": "F.Cu"},
    {"start": [22.5, 36.3], "end": [22.5, 34.5], "layer": "B.Cu"},
    {"start": [22.5, 34.5], "end": [22.5, 34.2], "layer": "F.Cu"}
  ]
}
```

Key: a Route is just a list of segments with start/end points. No grid alignment required — KiCad accepts any coordinates. The `kicad_import` snap-to-pad logic handles connecting the last segment to the exact pad position.

### 2. Current A* Pathfinder — `src/pathfinder.py`

**Pipeline** (`route_board`, line 676):
1. Build occupied grid from component pads, GND vias, and keepouts
2. Sort nets by priority then distance
3. For each net, call `route_net()` which:
   - Builds MST edges between pads (from `placement_scorer._build_mst_edges`)
   - Sorts edges shortest-first
   - For each edge, runs A* from source to destination
   - Marks routed path + clearance in occupied grid
4. Place GND vias at SMD pads
5. Return updated board with routes

**Net ordering** (line 720):
```python
nets_to_route.sort(key=lambda n: (n.priority, net_max_dist.get(n.name, 0.0)))
```
Power (priority=1) routes before signals (priority=10). Within priority, shortest nets first.

**A* details** (line 360):
- 8-direction moves (cardinal + diagonal)
- Costs: cardinal=10, diagonal=14 (scaled integers)
- Octile heuristic
- Via transitions cost `via_cost * 10` (default 5 → 50)
- Corner-cutting prevention on diagonals

**Critical limitation**: the A* has no concept of "route along the right edge" or "run through U1's center gap." It finds the shortest path through the grid, which often goes to B.Cu unnecessarily because F.Cu pad zones block the obvious direct path.

### 3. KiCad Import — `src/kicad_import.py`

**Coordinate conversion** (line 264):
```python
page_start = (seg.start[0] + origin_x, seg.start[1] + origin_y)
```
Board coords → KiCad page coords by adding the board origin.

**Pad snapping** (line 267-268):
```python
page_start = _snap_to_pad(page_start[0], page_start[1], route.net, pad_map)
```
Any segment endpoint within 0.5mm of a pad on the same net gets snapped to the exact pad position. This means the trace tool can use approximate coordinates and import will fix the last bit.

**Zone fill stripping** (line 40): `filled_polygon` nodes are stripped so KiCad recalculates the ground pour around new traces.

**KiCad 7+ format**: Uses `(net "netname")` strings, not numbers. UUIDs on every segment/via.

### 4. Agent Skill Commands — `.claude/commands/`

**`pcb-optimize.md`**: Full autonomous loop — export → placement optimization → route → DRC → export. The agent reads tool outputs and decides what to change at each phase. Currently calls `pathfinder` as a black box.

**`pcb-route.md`**: Routing-only — runs conflict analysis, pathfinder, DRC validation, retries failed nets with different via costs. The agent's reasoning is limited to "try lower via cost" or "try single-layer."

**`pcb-constrain.md`**: Conversational constraint elicitation. Interesting model for the agent-driven routing — shows how the agent can have a dialog about design intent then encode it as structured data.

### 5. Board Analysis Tools

**`placement_scorer.py`**:
- `_build_mst_edges(board)` — MST per net (Kruskal's, Manhattan distance)
- `_analyze_channels(board)` — channel capacity between adjacent component pairs
- `_comp_abs_bbox(comp)` — absolute component bounding box
- Composite score (crossings, wirelength, channel capacity, pin escape)

**`conflict_analyzer.py`**:
- `analyze_conflicts(board)` → net difficulty scores, bottleneck channels, routing order
- Could be extended to identify routing corridors and open spaces

**`kicad_drc.py`**:
- `run_kicad_drc(path)` → violations, unconnected items, problem nets
- Already integrated for validation feedback loop

### 6. Kegmon Board Topology — Why +3.3V/AVDD/GPIO12 Fail

**Board**: 30.0 × 80.1 mm, 2-layer

**+3.3V** (8 pads, the hardest net):
```
Pads: C5.1(22.5,36.5), C1.1(22.8,25.6), J4.3(4.8,30.9),
      JP1.3(22.6,42.0), J2.1(26.4,45.0), U1.1(22.4,28.1),
      U1.16(22.4,34.3), R1.1(8.1,30.9)
```
Most pads are along the right side (x≈22-26) from y=25 to y=45. A human would route a vertical trunk at x≈22-23 on F.Cu from C1 down to JP1/J2, with short branches to each pad. The A* instead routes scattered paths through B.Cu.

**Net-(U1-AVDD)** (3 pads):
```
Pads: C2.1(20.4,25.6), U1.3(19.9,28.1), J1.2(11.8,2.0)
```
C2→U1 is short and easy. The hard part is the long run from U1 area up to J1 (RJ45) at the top of the board. Needs to go vertically up the center of the board.

**Net-(J2-GPIO12)** (3 pads):
```
Pads: J4.2(4.8,33.5), J2.7(26.4,60.2), R1.2(8.1,32.7)
```
R1↔J4 is short (left side). The hard part is J2.7 at (26.4, 60.2) — way down the right side, needing a long route to reach R1 on the left.

**Why they fail**: The A* routes shorter nets first, consuming clearance around the U1 area. By the time these long nets try to route, all nearby F.Cu channels are blocked by clearance inflation from earlier routes. The A* has no concept of "reserve space for the power trunk."

## Existing Patterns to Follow

### Agent skill pattern (from `pcb-constrain.md`)
The agent has a conversation, collects structured data, writes it as JSON, then applies it via a Python tool. This same pattern works for routing:
1. Agent analyzes board topology (using scorer/analyzer tools)
2. Agent decides routing plan (which nets, which corridors, which layer)
3. Agent writes route plan as structured data (waypoints per net)
4. Python tool converts waypoints into segments/vias in board.json

### board.json as universal interchange
All tools read/write board.json. The trace tool should follow this pattern — read board.json, add routes/vias, write board.json back. The same `kicad_import` handles the final conversion.

### Pad snapping in import
Any trace endpoint within 0.5mm of a pad gets snapped. The agent can use approximate coordinates and import fixes them.

## Dependencies & External APIs

- **numpy** — only used by pathfinder for occupancy grid. New trace tool could be pure Python.
- **kicad-cli** — available at `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` for DRC validation.
- No external routing APIs or PCB libraries needed — everything is custom.

## Gaps & Open Questions

### 1. Agent's board understanding
The agent needs to "see" the board to make routing decisions. Current tools provide:
- Component positions and pad positions (from board.json)
- MST edges (from placement_scorer)
- Channel capacity and difficulty scores (from conflict_analyzer)

**Gap**: No tool provides "available routing corridors" — open F.Cu/B.Cu areas where traces can run. The agent needs spatial awareness: where are the clear paths between components?

**Possible approach**: A "corridor finder" tool that outputs: "There's a 4mm-wide F.Cu corridor from (22, 25) to (22, 45) along the right side" or "U1's center gap is 4.4mm wide between pad rows, from (13.6, 28) to (22.4, 34)."

### 2. Point-to-point trace tool API design
How should the agent specify routes? Options:

**Option A: Waypoint lists**
```json
{
  "net": "+3.3V",
  "waypoints": [[22.5, 25.6], [22.5, 34.3], [22.5, 42.0], [26.4, 45.0]],
  "layer": "F.Cu",
  "width_mm": 0.5
}
```
Agent gives ordered waypoints; tool generates segments between consecutive pairs. Layer changes require explicit via waypoints.

**Option B: Trunk + branches**
```json
{
  "net": "+3.3V",
  "trunk": {"start": [22.5, 25.0], "end": [22.5, 45.0], "layer": "F.Cu"},
  "branches": [
    {"from_y": 25.6, "to_pad": "C1.1"},
    {"from_y": 34.3, "to_pad": "U1.16"},
    {"from_y": 42.0, "to_pad": "JP1.3"}
  ]
}
```
More structured; tool figures out branch segments.

**Option C: Direct segment/via specification**
Agent writes the exact segments and vias — most control, most work for the agent.

**Recommendation**: Start with Option A (waypoints) — simple, flexible, the agent can learn to use it effectively. The tool handles pad snapping and grid alignment.

### 3. Clearance validation
The point-to-point tool needs to check that the route doesn't violate clearance with existing copper. Options:
- Pre-check against the occupancy grid before placing
- Place optimistically, then run KiCad DRC and iterate
- Both: quick internal check + KiCad DRC for final validation

### 4. Interaction model
Should the agent route one net at a time (seeing the result before planning the next), or plan all routes upfront?

**One-at-a-time** is safer — the agent sees what space is available after each route. But slower.
**Batch** is faster but risks conflicts. The A* fallback handles conflicts.

**Recommendation**: Agent plans the 2-3 hardest nets (power, long signals) one at a time with deliberation. Remaining easy nets go through A* autorouter. Best of both worlds.

### 5. CLAUDE.md update
The routing convention note says "Manhattan (90°) only. No 45° traces." — this is already outdated since we added diagonal routing. Needs update.

## Suggested Scope

### Implementation order:

1. **`src/trace_tool.py`** — Point-to-point trace tool
   - Input: net name, waypoints list, layer, width
   - Output: Route with Segments + Vias in board.json format
   - Validates basic clearance (no same-net overlaps)
   - Handles pad snapping and grid alignment
   - CLI: `python -m src.trace_tool board.json --plan route_plan.json -o board.json`

2. **`src/board_analyzer.py`** — Board spatial analysis for agent
   - Available corridors (open routing channels with dimensions)
   - Net pad map with human-readable descriptions
   - Obstacle map (where components/vias block each layer)
   - CLI: `python -m src.board_analyzer board.json` (JSON output for agent)

3. **`.claude/commands/pcb-route-strategic.md`** — Agent routing skill
   - Phase 1: Analyze board (board_analyzer + conflict_analyzer)
   - Phase 2: Plan power routes (agent decides waypoints using analysis)
   - Phase 3: Place power routes (trace_tool)
   - Phase 4: Autoroute remaining signals (pathfinder for easy nets)
   - Phase 5: Validate (kicad_drc) and iterate

4. **Update existing tools**
   - `pathfinder.py` — add `--skip-nets` flag to skip agent-routed nets
   - `CLAUDE.md` — update routing conventions (45° supported, agent-driven strategy)
   - `pcb-optimize.md` — integrate strategic routing into the optimization loop
