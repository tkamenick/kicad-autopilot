"""A* grid router for PCB traces.

Operates on the board.json intermediate format. Routes all non-ground nets
using Manhattan moves on a 2-layer (F.Cu / B.Cu) occupancy grid.

Multi-terminal nets use a Steiner tree approximation: build a Manhattan MST,
then route each edge in order of increasing length. Already-routed path cells
are marked occupied for subsequent edges, preventing overlaps.

CLI:
    python src/pathfinder.py board.json
    python src/pathfinder.py board.json --net SPI_CLK
    python src/pathfinder.py board.json --nets "SPI_CLK,SPI_MOSI"
    python src/pathfinder.py board.json -o routed.json
"""
from __future__ import annotations

import argparse
import heapq
import math
from pathlib import Path
from typing import Optional

import numpy as np

from src.schema import (
    Board, Component, Route, Segment, Via, load_board, save_board,
)
from src.placement_scorer import _build_mst_edges, _comp_abs_bbox

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LAYERS = ["F.Cu", "B.Cu"]   # index 0 = F.Cu, index 1 = B.Cu
VIA_COST = 5                 # grid-cell cost for a layer change
EDGE_CLEARANCE_CELLS = 2     # cells to keep clear from board boundary


# ---------------------------------------------------------------------------
# Cell ↔ coordinate helpers
# ---------------------------------------------------------------------------

def _cell(coord: float, grid: float) -> int:
    """Board coordinate → grid cell index."""
    return int(round(coord / grid))


def _coord(cell: int, grid: float) -> float:
    """Grid cell index → board coordinate (rounded to 4 dp)."""
    return round(cell * grid, 4)


# ---------------------------------------------------------------------------
# All pad positions (never block pad cells)
# ---------------------------------------------------------------------------

def _all_pad_cells(board: Board) -> set[tuple[int, int]]:
    """Return set of (row, col) for every electrically-connected pad.

    Only pads with a net are included — unconnected pads (e.g., mounting
    holes) are treated as obstacles, not routing targets.
    """
    grid = board.grid_step
    cells: set[tuple[int, int]] = set()
    for comp in board.components.values():
        for pad in comp.pads:
            if not pad.net:
                continue
            px, py = comp.pad_abs_position(pad)
            cells.add((_cell(py, grid), _cell(px, grid)))
    return cells


def _obstacle_exempt_cells(board: Board) -> set[tuple[int, int]]:
    """Return cells exempt from component-bbox obstacle marking.

    Includes actual pad cells PLUS escape corridors (cardinal lines from
    each pad to the edge of its component bbox).  Used only during initial
    ``_build_occupied`` so traces can physically reach pads inside large
    connectors.  Unlike ``_all_pad_cells``, these corridor cells CAN be
    blocked later by ``_mark_path`` after a route passes through them.

    Only pads with a net get corridors — unconnected pads (mounting holes)
    remain fully blocked.
    """
    grid = board.grid_step
    cells: set[tuple[int, int]] = set()
    for comp in board.components.values():
        ax0, ay0, ax1, ay1 = _comp_abs_bbox(comp)
        bc0, br0 = _cell(ax0, grid), _cell(ay0, grid)
        bc1, br1 = _cell(ax1, grid), _cell(ay1, grid)
        for pad in comp.pads:
            if not pad.net:
                continue
            px, py = comp.pad_abs_position(pad)
            r, c = _cell(py, grid), _cell(px, grid)
            cells.add((r, c))
            for er in range(r - 1, br0 - 2, -1):
                cells.add((er, c))
            for er in range(r + 1, br1 + 2):
                cells.add((er, c))
            for ec in range(c - 1, bc0 - 2, -1):
                cells.add((r, ec))
            for ec in range(c + 1, bc1 + 2):
                cells.add((r, ec))
    return cells


# ---------------------------------------------------------------------------
# Obstacle grid construction
# ---------------------------------------------------------------------------

def _build_occupied(board: Board) -> np.ndarray:
    """Build the initial occupancy grid from board geometry.

    Shape: (2, rows, cols) — axis 0 is layer (F.Cu=0, B.Cu=1).
    True = cell blocked. Pad cells are never marked blocked.
    """
    grid = board.grid_step
    outline_xs = [pt[0] for pt in board.board_outline]
    outline_ys = [pt[1] for pt in board.board_outline]
    min_x, max_x = min(outline_xs), max(outline_xs)
    min_y, max_y = min(outline_ys), max(outline_ys)

    cols = _cell(max_x, grid) + EDGE_CLEARANCE_CELLS + 2
    rows = _cell(max_y, grid) + EDGE_CLEARANCE_CELLS + 2
    occupied = np.zeros((2, rows, cols), dtype=bool)

    board_min_col = _cell(min_x, grid)
    board_max_col = _cell(max_x, grid)
    board_min_row = _cell(min_y, grid)
    board_max_row = _cell(max_y, grid)

    exempt_cells = _obstacle_exempt_cells(board)

    # Block cells outside board boundary + edge clearance
    safe_r0 = board_min_row + EDGE_CLEARANCE_CELLS
    safe_r1 = board_max_row - EDGE_CLEARANCE_CELLS
    safe_c0 = board_min_col + EDGE_CLEARANCE_CELLS
    safe_c1 = board_max_col - EDGE_CLEARANCE_CELLS

    for layer_idx in range(2):
        for r in range(rows):
            for c in range(cols):
                if r < safe_r0 or r > safe_r1 or c < safe_c0 or c > safe_c1:
                    occupied[layer_idx, r, c] = True

    # Block component bounding boxes (but never pad cells)
    for comp in board.components.values():
        ax0, ay0, ax1, ay1 = _comp_abs_bbox(comp)
        c0 = _cell(ax0, grid)
        r0 = _cell(ay0, grid)
        c1 = _cell(ax1, grid)
        r1 = _cell(ay1, grid)
        is_thru = any(p.layer == "*.Cu" for p in comp.pads)
        layers_to_mark = [0, 1] if is_thru else [0 if comp.layer == "F.Cu" else 1]
        for layer_idx in layers_to_mark:
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    if 0 <= r < rows and 0 <= c < cols:
                        if (r, c) not in exempt_cells:
                            occupied[layer_idx, r, c] = True

    # Block keepout zones
    for keepout in board.keepouts:
        layer_indices: list[int] = []
        for lname in keepout.layers:
            if lname == "*.Cu":
                layer_indices = [0, 1]
                break
            if lname in LAYERS:
                layer_indices.append(LAYERS.index(lname))
        if keepout.type == "rect" and keepout.rect is not None:
            kx, ky, kw, kh = keepout.rect
            c0 = _cell(kx, grid)
            r0 = _cell(ky, grid)
            c1 = _cell(kx + kw, grid)
            r1 = _cell(ky + kh, grid)
            for layer_idx in layer_indices:
                for r in range(r0, r1 + 1):
                    for c in range(c0, c1 + 1):
                        if 0 <= r < rows and 0 <= c < cols and (r, c) not in exempt_cells:
                            occupied[layer_idx, r, c] = True

    # Block existing route segments (only actual pad cells are exempt here,
    # not escape corridors — existing routes should block corridor cells)
    actual_pads = _all_pad_cells(board)
    for route in board.routes:
        for seg in route.segments:
            if seg.layer not in LAYERS:
                continue
            layer_idx = LAYERS.index(seg.layer)
            sc = _cell(seg.start[0], grid)
            sr = _cell(seg.start[1], grid)
            ec = _cell(seg.end[0], grid)
            er = _cell(seg.end[1], grid)
            if sc == ec:
                for r in range(min(sr, er), max(sr, er) + 1):
                    if 0 <= r < rows and 0 <= sc < cols and (r, sc) not in actual_pads:
                        occupied[layer_idx, r, sc] = True
            elif sr == er:
                for c in range(min(sc, ec), max(sc, ec) + 1):
                    if 0 <= sr < rows and 0 <= c < cols and (sr, c) not in actual_pads:
                        occupied[layer_idx, sr, c] = True

    return occupied


# ---------------------------------------------------------------------------
# Occupancy marking helpers
# ---------------------------------------------------------------------------

def _mark_via_area(occupied: np.ndarray, row: int, col: int,
                   pad_cells: set[tuple[int, int]]) -> None:
    """Mark a 3×3 cell area on both layers around a via (excludes pad cells)."""
    rows, cols = occupied.shape[1], occupied.shape[2]
    for dr in range(-1, 2):
        for dc in range(-1, 2):
            r, c = row + dr, col + dc
            if (r, c) in pad_cells:
                continue
            if 0 <= r < rows and 0 <= c < cols:
                occupied[0, r, c] = True
                occupied[1, r, c] = True


def _mark_path(occupied: np.ndarray, path: list[tuple[int, int, int]],
               pad_cells: set[tuple[int, int]]) -> None:
    """Mark path cells and 1-cell clearance zone occupied on the trace layer.

    The clearance inflation ensures a minimum 1-grid-step (0.3 mm) gap
    between traces of different nets, satisfying typical PCB clearance rules.
    Via transition points are expanded to 3×3 on both layers.

    Cells adjacent to pad_cells are exempt from clearance marking so that
    multiple routes can converge on the same pad (e.g., multi-terminal nets
    where several MST edges share a common pad).
    """
    rows, cols = occupied.shape[1], occupied.shape[2]
    # Build set of cells adjacent to any pad (exempt from clearance)
    pad_neighbors: set[tuple[int, int]] = set()
    for pr, pc in pad_cells:
        pad_neighbors.add((pr, pc))
        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            pad_neighbors.add((pr + dr, pc + dc))

    for r, c, l in path:
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                nr, nc = r + dr, c + dc
                if (nr, nc) in pad_neighbors:
                    continue
                if 0 <= nr < rows and 0 <= nc < cols:
                    occupied[l, nr, nc] = True
    # Expand via transition points (both layers, 3×3)
    for i in range(1, len(path)):
        if path[i][2] != path[i - 1][2]:
            r, c = path[i][0], path[i][1]
            _mark_via_area(occupied, r, c, pad_cells)


# ---------------------------------------------------------------------------
# A* search
# ---------------------------------------------------------------------------

def _astar(
    src_cells: list[tuple[int, int, int]],  # (row, col, layer) start states
    dst_cells: set[tuple[int, int, int]],   # (row, col, layer) goal states
    occupied: np.ndarray,
    via_cost: int,
    no_via_cells: Optional[set[tuple[int, int]]] = None,
) -> Optional[list[tuple[int, int, int]]]:
    """A* from any src_cell to any dst_cell. Returns path or None.

    no_via_cells: pad positions where layer transitions are forbidden.
    Forces vias to be placed at least 1 cell away from pads.
    """
    rows, cols = occupied.shape[1], occupied.shape[2]
    if no_via_cells is None:
        no_via_cells = set()
    dst_rc = [(r, c) for r, c, _ in dst_cells]

    def heuristic(r: int, c: int) -> int:
        return min(abs(r - dr) + abs(c - dc) for dr, dc in dst_rc)

    # heap entries: (f_score, g_score, row, col, layer)
    open_set: list[tuple[int, int, int, int, int]] = []
    g_score: dict[tuple[int, int, int], int] = {}
    came_from: dict[tuple[int, int, int], tuple[int, int, int]] = {}

    for r, c, l in src_cells:
        if not (0 <= r < rows and 0 <= c < cols):
            continue
        g = 0
        h = heuristic(r, c)
        heapq.heappush(open_set, (h, g, r, c, l))
        g_score[(r, c, l)] = 0

    while open_set:
        f, g, r, c, l = heapq.heappop(open_set)
        state = (r, c, l)

        if g > g_score.get(state, 999_999_999):
            continue  # stale entry

        if state in dst_cells:
            path: list[tuple[int, int, int]] = [state]
            while state in came_from:
                state = came_from[state]
                path.append(state)
            path.reverse()
            return path

        # 4 cardinal moves on same layer
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if occupied[l, nr, nc]:
                continue
            ns = (nr, nc, l)
            ng = g + 1
            if ng < g_score.get(ns, 999_999_999):
                g_score[ns] = ng
                came_from[ns] = state
                heapq.heappush(open_set, (ng + heuristic(nr, nc), ng, nr, nc, l))

        # Via transition (same cell, other layer) — forbidden at pad cells
        nl = 1 - l
        if not occupied[nl, r, c] and (r, c) not in no_via_cells:
            ns = (r, c, nl)
            ng = g + via_cost
            if ng < g_score.get(ns, 999_999_999):
                g_score[ns] = ng
                came_from[ns] = state
                heapq.heappush(open_set, (ng + heuristic(r, c), ng, r, c, nl))

    return None  # no path found


# ---------------------------------------------------------------------------
# Path → Segment + Via objects
# ---------------------------------------------------------------------------

def _path_to_segments_vias(
    path: list[tuple[int, int, int]],
    net_name: str,
    grid: float,
    via_drill: float,
) -> tuple[list[Segment], list[Via]]:
    """Convert cell path to Segment and Via objects.

    Breaks into new segments at direction changes and layer changes,
    producing only horizontal/vertical (Manhattan) trace segments.
    """
    if len(path) < 2:
        return [], []

    segments: list[Segment] = []
    vias: list[Via] = []

    def _emit_seg(start: tuple[int, int, int], end: tuple[int, int, int]) -> None:
        if (start[0], start[1]) != (end[0], end[1]):
            segments.append(Segment(
                start=(_coord(start[1], grid), _coord(start[0], grid)),
                end=(_coord(end[1], grid), _coord(end[0], grid)),
                layer=LAYERS[start[2]],
            ))

    seg_start = path[0]
    prev_dr = path[1][0] - path[0][0]
    prev_dc = path[1][1] - path[0][1]

    for i in range(1, len(path)):
        prev = path[i - 1]
        curr = path[i]
        dr = curr[0] - prev[0]
        dc = curr[1] - prev[1]

        if curr[2] != prev[2]:
            # Layer change: close current segment, emit via
            _emit_seg(seg_start, prev)
            vias.append(Via(
                position=(_coord(prev[1], grid), _coord(prev[0], grid)),
                net=net_name,
                drill_mm=via_drill,
            ))
            seg_start = curr
            prev_dr, prev_dc = 0, 0  # reset direction for next step
        elif (dr, dc) != (prev_dr, prev_dc):
            # Direction changed: close current segment at prev
            _emit_seg(seg_start, prev)
            seg_start = prev
            prev_dr, prev_dc = dr, dc
        # else: same direction, continue extending

    # Close final segment
    _emit_seg(seg_start, path[-1])

    return segments, vias


# ---------------------------------------------------------------------------
# Net pad lookup
# ---------------------------------------------------------------------------

def _pad_layer_index(comp: Component, px: float, py: float) -> list[int]:
    """Return which layer indices a pad at this absolute position is on."""
    grid = 0.001  # tight tolerance for position matching
    for pad in comp.pads:
        ax, ay = comp.pad_abs_position(pad)
        if abs(ax - px) < 0.01 and abs(ay - py) < 0.01:
            if pad.layer == "*.Cu":
                return [0, 1]
            if pad.layer in LAYERS:
                return [LAYERS.index(pad.layer)]
    return [0, 1]  # default: both layers


def _net_pad_positions(board: Board, net_name: str) -> list[tuple[float, float, list[int]]]:
    """Return list of (x, y, layer_indices) for each pad on the given net."""
    positions: list[tuple[float, float, list[int]]] = []
    for comp in board.components.values():
        for pad in comp.pads:
            if pad.net != net_name:
                continue
            px, py = comp.pad_abs_position(pad)
            if pad.layer == "*.Cu":
                layers = [0, 1]
            elif pad.layer in LAYERS:
                layers = [LAYERS.index(pad.layer)]
            else:
                layers = [0, 1]
            positions.append((px, py, layers))
    return positions


# ---------------------------------------------------------------------------
# Single-net routing
# ---------------------------------------------------------------------------

def route_net(
    board: Board,
    net_name: str,
    occupied: np.ndarray,
    pad_cells: set[tuple[int, int]],
    via_cost: int = VIA_COST,
) -> tuple[list[Segment], list[Via], int]:
    """Route a single net using MST edge decomposition.

    Updates `occupied` in-place. Returns (segments, vias, failed_edge_count).
    """
    grid = board.grid_step
    net = board.nets.get(net_name)
    via_drill = board.rules.via_drill_mm

    mst_by_net = _build_mst_edges(board)
    edges = mst_by_net.get(net_name, [])
    if not edges:
        return [], [], 0

    # Sort edges by Manhattan distance (shortest first)
    def manhattan(p1: tuple[float, float], p2: tuple[float, float]) -> float:
        return abs(p1[0] - p2[0]) + abs(p1[1] - p2[1])

    edges_sorted = sorted(edges, key=lambda e: manhattan(e[0], e[1]))

    all_segments: list[Segment] = []
    all_vias: list[Via] = []
    routed_edges = 0
    failed_edges = 0

    # Build a lookup: (row, col) → layer_indices for this net's pads
    # Build lookup: this net's pad cells and their layer indices
    pad_pos_map: dict[tuple[int, int], list[int]] = {}
    my_pads: set[tuple[int, int]] = set()
    for comp in board.components.values():
        for pad in comp.pads:
            if pad.net != net_name:
                continue
            px, py = comp.pad_abs_position(pad)
            pr, pc = _cell(py, grid), _cell(px, grid)
            if pad.layer == "*.Cu":
                layers = [0, 1]
            elif pad.layer in LAYERS:
                layers = [LAYERS.index(pad.layer)]
            else:
                layers = [0, 1]
            pad_pos_map[(pr, pc)] = layers
            my_pads.add((pr, pc))

    # Temporarily block other nets' pad cells so we don't route through them
    other_pads: list[tuple[int, int]] = []
    rows, cols = occupied.shape[1], occupied.shape[2]
    for rc in pad_cells:
        if rc not in my_pads:
            r, c = rc
            if 0 <= r < rows and 0 <= c < cols:
                # Block on both layers if not already blocked
                if not occupied[0, r, c] or not occupied[1, r, c]:
                    other_pads.append(rc)
                    occupied[0, r, c] = True
                    occupied[1, r, c] = True

    for p1, p2 in edges_sorted:
        r1, c1 = _cell(p1[1], grid), _cell(p1[0], grid)
        r2, c2 = _cell(p2[1], grid), _cell(p2[0], grid)

        src_layers = pad_pos_map.get((r1, c1), [0, 1])
        dst_layers = pad_pos_map.get((r2, c2), [0, 1])

        src_cells = [(r1, c1, l) for l in src_layers]
        dst_cells = {(r2, c2, l) for l in dst_layers}

        path = _astar(src_cells, dst_cells, occupied, via_cost, no_via_cells=my_pads)
        if path is None:
            path = _astar(
                [(r2, c2, l) for l in dst_layers],
                {(r1, c1, l) for l in src_layers},
                occupied, via_cost, no_via_cells=my_pads,
            )

        if path is not None:
            segs, vialist = _path_to_segments_vias(path, net_name, grid, via_drill)
            all_segments.extend(segs)
            all_vias.extend(vialist)
            _mark_path(occupied, path, pad_cells)
            routed_edges += 1
        else:
            failed_edges += 1

    # Restore other nets' pad cells (so future nets can still reach them)
    for r, c in other_pads:
        occupied[0, r, c] = False
        occupied[1, r, c] = False

    return all_segments, all_vias, failed_edges


# ---------------------------------------------------------------------------
# Board-level routing
# ---------------------------------------------------------------------------

def route_board(
    board: Board,
    via_cost: int = VIA_COST,
    only_nets: Optional[list[str]] = None,
) -> tuple[Board, list[str]]:
    """Route all non-ground nets. Returns (updated_board, failed_net_names).

    Net ordering: power (priority=1) → constrained_signal (2) → signal (10).
    Ground nets (class_='ground') are skipped — handled by B.Cu pour.

    If only_nets is given, routes only those net names (skip ground regardless).
    """
    occupied = _build_occupied(board)
    pad_cells = _all_pad_cells(board)

    # Compute max Manhattan distance per net (short nets route first)
    net_max_dist: dict[str, float] = {}
    pad_counts: dict[str, int] = {}
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for comp in board.components.values():
        for pad in comp.pads:
            if pad.net:
                pad_counts[pad.net] = pad_counts.get(pad.net, 0) + 1
                pos = comp.pad_abs_position(pad)
                net_pads.setdefault(pad.net, []).append(pos)
    for net_name, positions in net_pads.items():
        max_d = 0.0
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                d = abs(positions[i][0] - positions[j][0]) + abs(positions[i][1] - positions[j][1])
                if d > max_d:
                    max_d = d
        net_max_dist[net_name] = max_d

    # Determine which nets to route
    # Order: priority first, then shortest nets first within each priority
    # This ensures short decoupling connections (cap↔IC) route before long
    # cross-board signals, keeping F.Cu clear for critical short paths.
    nets_to_route = [
        n for n in board.nets.values()
        if n.class_ != "ground"
        and (only_nets is None or n.name in only_nets)
    ]
    nets_to_route.sort(key=lambda n: (n.priority, net_max_dist.get(n.name, 0.0)))

    # Place GND vias at F.Cu SMD pads (connects to B.Cu ground pour)
    gnd_vias: list[Via] = []
    via_drill = board.rules.via_drill_mm
    for net in board.nets.values():
        if net.class_ != "ground":
            continue
        for comp in board.components.values():
            for pad in comp.pads:
                if pad.net != net.name:
                    continue
                # Only F.Cu SMD pads need a via — through-hole pads already
                # connect to the pour through the plated hole.
                if pad.layer in ("*.Cu", "B.Cu"):
                    continue
                px, py = comp.pad_abs_position(pad)
                gnd_vias.append(Via(
                    position=(round(px, 4), round(py, 4)),
                    net=net.name,
                    drill_mm=via_drill,
                ))

    # Carry over existing routes; add GND vias only if not already present
    all_routes = list(board.routes)
    existing_via_positions = {(v.position, v.net) for v in board.vias}
    deduped_gnd_vias = [
        v for v in gnd_vias
        if (v.position, v.net) not in existing_via_positions
    ]
    all_vias = list(board.vias) + deduped_gnd_vias
    failed: list[str] = []

    for net in nets_to_route:
        segs, vialist, n_failed_edges = route_net(board, net.name, occupied, pad_cells, via_cost)

        if not segs and not vialist:
            if pad_counts.get(net.name, 0) >= 2:
                failed.append(net.name)
            continue

        if n_failed_edges > 0:
            failed.append(net.name)

        width = net.width_mm or board.rules.default_trace_width_mm
        if segs:
            all_routes.append(Route(net=net.name, width_mm=width, segments=segs))
        all_vias.extend(vialist)

    from dataclasses import replace
    updated = Board(
        board_outline=board.board_outline,
        grid_step=board.grid_step,
        rules=board.rules,
        components=board.components,
        nets=board.nets,
        keepouts=board.keepouts,
        routes=all_routes,
        vias=all_vias,
        pours=board.pours,
    )
    return updated, failed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="A* grid router for board.json")
    parser.add_argument("input", help="Input board.json")
    parser.add_argument("-o", "--output", default=None,
                        help="Output board.json (default: overwrite input)")
    parser.add_argument("--net", default=None,
                        help="Route a single net by name")
    parser.add_argument("--nets", default=None,
                        help="Comma-separated list of nets to route")
    parser.add_argument("--via-cost", type=int, default=VIA_COST,
                        help=f"Grid-cell cost for a via (default {VIA_COST})")
    args = parser.parse_args()

    board = load_board(args.input)

    only: Optional[list[str]] = None
    if args.net:
        only = [args.net]
    elif args.nets:
        only = [n.strip() for n in args.nets.split(",")]

    routed, failed = route_board(board, via_cost=args.via_cost, only_nets=only)

    n_segs = sum(len(r.segments) for r in routed.routes)
    n_vias = len(routed.vias)
    print(f"Routed: {n_segs} segments, {n_vias} vias")
    if failed:
        print(f"Failed nets ({len(failed)}): {', '.join(failed)}")

    out_path = args.output or args.input
    save_board(routed, out_path)
    print(f"Saved → {out_path}")


if __name__ == "__main__":
    main()
