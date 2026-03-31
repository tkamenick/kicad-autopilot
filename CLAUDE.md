# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-assisted PCB layout and routing system. Claude acts as the design reasoning engine (net classification, placement strategy, corridor assignment, routing order), while Python scripts handle deterministic computation (scoring, A* pathfinding, DRC, spatial analysis).

Test case: T-Display-S3 carrier board (2-layer, ~10-30 nets, small form factor).

## Commands

All scripts must be run as Python modules from the project root (so `src` is importable).
One-time setup: `pip install -e .` makes `python -m src.xxx` equivalent to `python src/xxx.py`.

```bash
# Run all tests
python -m pytest tests/

# Run a single test file
python -m pytest tests/test_sexpr_parser.py -v

# Export KiCad board to board.json
python -m src.kicad_export input.kicad_pcb -o board.json --grid 0.3

# Import board.json back to KiCad
python -m src.kicad_import board.json --base original.kicad_pcb -o routed.kicad_pcb

# Score placement quality
python -m src.placement_scorer board.json

# Sweep placement variants
python -m src.placement_sweeper board.json --moves moves.json --top 10

# Apply placement constraints
python -m src.apply_constraints board.json constraints.json -o board.json

# Route nets
python -m src.pathfinder board.json --net "SPI_CLK"
python -m src.pathfinder board.json --nets "3V3,SPI_CLK,SPI_MOSI"

# Check design rules
python -m src.drc_checker board.json

# Analyze routing conflicts
python -m src.conflict_analyzer board.json

# Render board as SVG
python -m src.visualizer board.json -o board.svg --show-ratsnest --show-corridors
```

## Architecture

All tools communicate via a single `board.json` intermediate format. The pipeline is:

```
.kicad_pcb → [kicad_export] → board.json → [tools] → board.json → [kicad_import] → .kicad_pcb
```

**Key scripts in `src/`:**
- `sexpr_parser.py` — Parses KiCad's s-expression file format (recursive descent, no KiCad Python bindings dependency)
- `kicad_export.py` / `kicad_import.py` — Round-trip conversion between `.kicad_pcb` and `board.json`
- `placement_scorer.py` — Computes placement quality metrics (wirelength, crossings, channel capacity, composite score 0-100)
- `placement_sweeper.py` — Generates and scores placement variants given move specifications
- `pathfinder.py` — A* grid router with multi-layer/via support and Steiner tree approximation for multi-terminal nets
- `drc_checker.py` — Design rule validation (clearance, shorts, unrouted nets, trace width, edge clearance)
- `conflict_analyzer.py` — Pre-routing bottleneck and corridor competition analysis
- `visualizer.py` — SVG renderer for board state visualization
- `schema.py` — board.json validation and types

## Key Conventions

- **Coordinates:** All in millimeters, snapped to 0.3mm grid. Origin is board top-left corner (translated from KiCad's page-origin coordinate system during export).
- **Grid snapping:** `round(round(value / grid) * grid, 4)` — all board.json coordinates must be grid-aligned.
- **Routing:** Manhattan (90°) only. No 45° traces.
- **Via cost:** Default 5 grid cells equivalent (1.5mm wirelength penalty) to discourage unnecessary layer changes.
- **Ground pour:** Modeled as B.Cu fill everywhere except under routes and keepouts. DRC must verify pour contiguity (no isolated islands).
- **Dependencies:** Minimal — stdlib plus optionally numpy. No KiCad Python bindings required (s-expression parser is standalone).
- **Layer model:** 2-layer (F.Cu, B.Cu). Ground pour lives on B.Cu; minimize vias to preserve return current integrity.

## Build Order (Milestones)

1. **See the board:** sexpr_parser → kicad_export → visualizer
2. **Score placement:** placement_scorer → placement_sweeper
3. **Route:** pathfinder → drc_checker → conflict_analyzer
4. **Write back:** kicad_import
5. **Iterate:** Interactive Claude reasoning loop with all tools
