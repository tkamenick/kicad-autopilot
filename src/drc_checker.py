"""Post-routing Design Rule Check (DRC).

Checks:
  1. unrouted    — nets with pads on ≥2 components that have no connecting route
  2. edge_clearance — trace endpoints within edge_clearance_mm of board boundary
  3. short        — two different nets sharing a grid cell (cell→net collision)
  4. trace_width  — segments narrower than rules.min_clearance_mm (catches accidental 0-width)

CLI:
    python src/drc_checker.py board.json
    python src/drc_checker.py board.json --edge-clearance 0.5
    Exits with code 1 if any 'error'-severity violations exist.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.schema import Board, Segment, load_board
from src.placement_scorer import _build_mst_edges, _comp_abs_bbox


# ---------------------------------------------------------------------------
# Violation dataclass
# ---------------------------------------------------------------------------

@dataclass
class DRCViolation:
    type: str            # "unrouted" | "edge_clearance" | "short" | "trace_width"
    severity: str        # "error" | "warning"
    net_a: str
    net_b: str = ""
    location: tuple[float, float] = field(default_factory=lambda: (0.0, 0.0))
    message: str = ""


def drc_to_dict(v: DRCViolation) -> dict:
    return {
        "type": v.type,
        "severity": v.severity,
        "net_a": v.net_a,
        "net_b": v.net_b,
        "location": list(v.location),
        "message": v.message,
    }


# ---------------------------------------------------------------------------
# Cell helpers (same convention as pathfinder)
# ---------------------------------------------------------------------------

def _cell(coord: float, grid: float) -> int:
    return int(round(coord / grid))


def _cells_on_segment(seg: Segment, grid: float) -> list[tuple[int, int]]:
    """All grid cells covered by a segment (cardinal + diagonal)."""
    sc = _cell(seg.start[0], grid)
    sr = _cell(seg.start[1], grid)
    ec = _cell(seg.end[0], grid)
    er = _cell(seg.end[1], grid)
    cells: list[tuple[int, int]] = []
    if sc == ec:
        for r in range(min(sr, er), max(sr, er) + 1):
            cells.append((r, sc))
    elif sr == er:
        for c in range(min(sc, ec), max(sc, ec) + 1):
            cells.append((sr, c))
    else:
        # Bresenham-style line for diagonal/arbitrary segments
        steps = max(abs(ec - sc), abs(er - sr))
        for i in range(steps + 1):
            t = i / steps if steps > 0 else 0
            cells.append((round(sr + t * (er - sr)), round(sc + t * (ec - sc))))
    return cells


# ---------------------------------------------------------------------------
# Check 1: Unrouted nets
# ---------------------------------------------------------------------------

def _check_unrouted(board: Board) -> list[DRCViolation]:
    """Report nets where at least one MST edge has no routed path."""
    violations: list[DRCViolation] = []
    grid = board.grid_step

    # Build cell → nets set from all routed segments + vias
    routed_cells: dict[tuple[int, int], set[str]] = {}
    for route in board.routes:
        for seg in route.segments:
            for cell in _cells_on_segment(seg, grid):
                routed_cells.setdefault(cell, set()).add(route.net)
    for via in board.vias:
        vc = (_cell(via.position[1], grid), _cell(via.position[0], grid))
        routed_cells.setdefault(vc, set()).add(via.net)

    # Check connectivity: for each net, verify all pad pairs in MST are connected
    mst = _build_mst_edges(board)
    for net_name, edges in mst.items():
        net = board.nets.get(net_name)
        if net and net.class_ == "ground":
            continue  # ground handled by pour

        # Build a cell-level adjacency graph for this net's routed segments
        net_adj: dict[tuple[int, int], set[tuple[int, int]]] = {}
        for route in board.routes:
            if route.net != net_name:
                continue
            for seg in route.segments:
                cells = _cells_on_segment(seg, grid)
                for i in range(len(cells) - 1):
                    a, b = cells[i], cells[i + 1]
                    net_adj.setdefault(a, set()).add(b)
                    net_adj.setdefault(b, set()).add(a)
        for via in board.vias:
            if via.net != net_name:
                continue
            vc = (_cell(via.position[1], grid), _cell(via.position[0], grid))
            # Vias connect adjacent cells on both layers (simplified: treat as self-connected)
            net_adj.setdefault(vc, set())

        # For each MST edge, check if endpoints are connected via BFS
        for p1, p2 in edges:
            c1 = (_cell(p1[1], grid), _cell(p1[0], grid))
            c2 = (_cell(p2[1], grid), _cell(p2[0], grid))
            if c1 == c2:
                continue

            # BFS from c1 in net_adj
            if not net_adj:
                connected = False
            else:
                visited = {c1}
                queue = [c1]
                connected = False
                while queue:
                    cur = queue.pop()
                    if cur == c2:
                        connected = True
                        break
                    for nb in net_adj.get(cur, set()):
                        if nb not in visited:
                            visited.add(nb)
                            queue.append(nb)

            if not connected:
                mid_x = (p1[0] + p2[0]) / 2
                mid_y = (p1[1] + p2[1]) / 2
                violations.append(DRCViolation(
                    type="unrouted",
                    severity="error",
                    net_a=net_name,
                    location=(mid_x, mid_y),
                    message=f"Net '{net_name}': no route between ({p1[0]:.2f},{p1[1]:.2f}) "
                            f"and ({p2[0]:.2f},{p2[1]:.2f})",
                ))

    return violations


# ---------------------------------------------------------------------------
# Check 2: Edge clearance
# ---------------------------------------------------------------------------

def _check_edge_clearance(
    board: Board, edge_clearance_mm: float = 0.5
) -> list[DRCViolation]:
    """Report segment endpoints and via positions too close to board edges."""
    violations: list[DRCViolation] = []
    outline_xs = [pt[0] for pt in board.board_outline]
    outline_ys = [pt[1] for pt in board.board_outline]
    min_x, max_x = min(outline_xs), max(outline_xs)
    min_y, max_y = min(outline_ys), max(outline_ys)

    def _too_close(x: float, y: float) -> bool:
        return (x < min_x + edge_clearance_mm or x > max_x - edge_clearance_mm
                or y < min_y + edge_clearance_mm or y > max_y - edge_clearance_mm)

    for route in board.routes:
        for seg in route.segments:
            for pt in (seg.start, seg.end):
                if _too_close(pt[0], pt[1]):
                    violations.append(DRCViolation(
                        type="edge_clearance",
                        severity="warning",
                        net_a=route.net,
                        location=pt,
                        message=f"Net '{route.net}': trace endpoint ({pt[0]:.2f},{pt[1]:.2f}) "
                                f"within {edge_clearance_mm}mm of board edge",
                    ))

    for via in board.vias:
        if _too_close(via.position[0], via.position[1]):
            violations.append(DRCViolation(
                type="edge_clearance",
                severity="warning",
                net_a=via.net,
                location=via.position,
                message=f"Net '{via.net}': via at ({via.position[0]:.2f},{via.position[1]:.2f}) "
                        f"within {edge_clearance_mm}mm of board edge",
            ))

    return violations


# ---------------------------------------------------------------------------
# Check 3: Short circuits
# ---------------------------------------------------------------------------

def _check_shorts(board: Board) -> list[DRCViolation]:
    """Report cells occupied by segments from two different nets (short circuit)."""
    violations: list[DRCViolation] = []
    grid = board.grid_step

    # Map (layer, row, col) → first net name that occupied it
    cell_net: dict[tuple[str, int, int], str] = {}
    reported: set[frozenset[str]] = set()

    for route in board.routes:
        for seg in route.segments:
            cells = _cells_on_segment(seg, grid)
            for r, c in cells:
                key = (seg.layer, r, c)
                existing = cell_net.get(key)
                if existing is None:
                    cell_net[key] = route.net
                elif existing != route.net:
                    pair = frozenset({existing, route.net})
                    if pair not in reported:
                        reported.add(pair)
                        x = round(c * grid, 4)
                        y = round(r * grid, 4)
                        violations.append(DRCViolation(
                            type="short",
                            severity="error",
                            net_a=existing,
                            net_b=route.net,
                            location=(x, y),
                            message=f"Short circuit between '{existing}' and '{route.net}' "
                                    f"at ({x:.2f},{y:.2f}) on {seg.layer}",
                        ))

    return violations


# ---------------------------------------------------------------------------
# Check 4: Trace width
# ---------------------------------------------------------------------------

def _check_trace_width(board: Board) -> list[DRCViolation]:
    """Report segments with suspiciously narrow width (< min_clearance_mm)."""
    violations: list[DRCViolation] = []
    min_w = board.rules.min_clearance_mm
    for route in board.routes:
        if route.width_mm < min_w:
            violations.append(DRCViolation(
                type="trace_width",
                severity="error",
                net_a=route.net,
                message=f"Net '{route.net}': trace width {route.width_mm}mm < "
                        f"min clearance {min_w}mm",
            ))
    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _check_pour_connectivity(board: Board) -> list[DRCViolation]:
    """Check that all GND vias/pads can reach each other through the B.Cu pour.

    Builds a B.Cu occupancy grid, marks non-GND copper as blocked, then
    flood-fills from the first GND position. Any GND via/pad not reached
    is on an isolated copper island.
    """
    from collections import deque

    violations: list[DRCViolation] = []
    grid = board.grid_step
    outline_xs = [pt[0] for pt in board.board_outline]
    outline_ys = [pt[1] for pt in board.board_outline]
    if not outline_xs:
        return violations

    max_x, max_y = max(outline_xs), max(outline_ys)
    cols = int(round(max_x / grid)) + 4
    rows = int(round(max_y / grid)) + 4

    # B.Cu grid: True = blocked (non-GND copper or outside board)
    blocked = [[True] * cols for _ in range(rows)]

    # Open all cells inside the board
    min_c = int(round(min(outline_xs) / grid))
    max_c = int(round(max_x / grid))
    min_r = int(round(min(outline_ys) / grid))
    max_r = int(round(max_y / grid))
    for r in range(min_r, min(max_r + 1, rows)):
        for c in range(min_c, min(max_c + 1, cols)):
            blocked[r][c] = False

    # Block non-GND B.Cu traces (with 2-cell clearance to model pour gap)
    for route in board.routes:
        if route.net in ("GND", "AGND", "DGND", "PGND"):
            continue
        for seg in route.segments:
            if seg.layer != "B.Cu":
                continue
            sc = int(round(seg.start[0] / grid))
            sr = int(round(seg.start[1] / grid))
            ec = int(round(seg.end[0] / grid))
            er = int(round(seg.end[1] / grid))
            if sc == ec:
                for r in range(min(sr, er), max(sr, er) + 1):
                    for dr in range(-2, 3):
                        for dc in range(-2, 3):
                            nr, nc = r + dr, sc + dc
                            if 0 <= nr < rows and 0 <= nc < cols:
                                blocked[nr][nc] = True
            elif sr == er:
                for c in range(min(sc, ec), max(sc, ec) + 1):
                    for dr in range(-2, 3):
                        for dc in range(-2, 3):
                            nr, nc = sr + dr, c + dc
                            if 0 <= nr < rows and 0 <= nc < cols:
                                blocked[nr][nc] = True

    # Block non-GND vias (with clearance)
    for via in board.vias:
        if via.net in ("GND", "AGND", "DGND", "PGND"):
            continue
        vc = int(round(via.position[0] / grid))
        vr = int(round(via.position[1] / grid))
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                nr, nc = vr + dr, vc + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    blocked[nr][nc] = True

    # Collect all GND positions (vias + through-hole pads)
    gnd_positions: list[tuple[int, int, str]] = []  # (row, col, description)
    for via in board.vias:
        if via.net in ("GND", "AGND", "DGND", "PGND"):
            r = int(round(via.position[1] / grid))
            c = int(round(via.position[0] / grid))
            gnd_positions.append((r, c, f"GND via at ({via.position[0]:.2f},{via.position[1]:.2f})"))

    for comp in board.components.values():
        for pad in comp.pads:
            if pad.net not in ("GND", "AGND", "DGND", "PGND"):
                continue
            if pad.layer != "*.Cu":
                continue  # only through-hole pads connect to B.Cu pour
            px, py = comp.pad_abs_position(pad)
            r = int(round(py / grid))
            c = int(round(px / grid))
            gnd_positions.append((r, c, f"GND pad {comp.reference}.{pad.number} at ({px:.2f},{py:.2f})"))

    if len(gnd_positions) < 2:
        return violations

    # Flood-fill from the first GND position
    visited = set()
    queue = deque()
    start_r, start_c = gnd_positions[0][0], gnd_positions[0][1]
    if 0 <= start_r < rows and 0 <= start_c < cols:
        queue.append((start_r, start_c))
        visited.add((start_r, start_c))

    while queue:
        r, c = queue.popleft()
        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in visited and not blocked[nr][nc]:
                visited.add((nr, nc))
                queue.append((nr, nc))

    # Check if all GND positions are reachable
    for r, c, desc in gnd_positions[1:]:
        if (r, c) not in visited:
            violations.append(DRCViolation(
                type="pour_island",
                severity="error",
                net_a="GND",
                location=(round(c * grid, 4), round(r * grid, 4)),
                message=f"Isolated GND: {desc} not connected to main pour",
            ))

    return violations


def check_drc(
    board: Board,
    edge_clearance_mm: float = 0.5,
) -> list[DRCViolation]:
    """Run all DRC checks and return violations."""
    violations: list[DRCViolation] = []
    violations.extend(_check_unrouted(board))
    violations.extend(_check_edge_clearance(board, edge_clearance_mm))
    violations.extend(_check_shorts(board))
    violations.extend(_check_trace_width(board))
    violations.extend(_check_pour_connectivity(board))
    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run DRC on a routed board.json")
    parser.add_argument("board_json", help="Input board.json")
    parser.add_argument("--edge-clearance", type=float, default=0.5,
                        help="Edge clearance threshold in mm (default 0.5)")
    args = parser.parse_args()

    board = load_board(args.board_json)
    violations = check_drc(board, edge_clearance_mm=args.edge_clearance)

    if not violations:
        print("DRC passed — no violations")
    else:
        errors = [v for v in violations if v.severity == "error"]
        warnings = [v for v in violations if v.severity == "warning"]
        print(f"DRC: {len(errors)} errors, {len(warnings)} warnings")
        for v in violations:
            prefix = "ERROR" if v.severity == "error" else "WARN "
            print(f"  [{prefix}] {v.message}")

    has_errors = any(v.severity == "error" for v in violations)
    raise SystemExit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
