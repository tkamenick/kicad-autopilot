"""Placement quality scorer.

Given a board.json, computes metrics that quantify how good the component
placement is from a routing perspective.

Metrics:
  - ratsnest_crossings:     pairwise intersections between different-net MST edges
  - total_wirelength_mm:    sum of Manhattan MST edge lengths
  - weighted_wirelength_mm: same, weighted by net class (power=3x, constrained=2x, signal=1x)
  - channel_capacity:       corridor congestion analysis for adjacent component pairs
  - pin_escape_violations:  pads with ≥3/4 cardinal directions blocked
  - composite_score:        0–100, higher is better

CLI:
    python src/placement_scorer.py board.json
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from typing import Optional

from src.schema import Board, Component, Net, load_board, snap_to_grid


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChannelInfo:
    component_a: str
    component_b: str
    available_tracks: int   # floor(gap / (trace_width + min_clearance))
    required_tracks: int    # nets with pads on both components
    utilization: float      # required / max(1, available)
    oversubscribed: bool


@dataclass
class PlacementScore:
    ratsnest_crossings: int
    total_wirelength_mm: float
    weighted_wirelength_mm: float
    channel_capacity: dict[str, ChannelInfo]  # key: "A-B_h" or "A-B_v"
    pin_escape_violations: list[str]           # "REF:pad_num"
    constraint_violations: list[str]           # human-readable constraint violations
    composite_score: float                     # 0–100


# ---------------------------------------------------------------------------
# Net class weights for wirelength
# ---------------------------------------------------------------------------

_NET_CLASS_WEIGHT: dict[str, float] = {
    "ground": 0.0,
    "power": 3.0,
    "constrained_signal": 2.0,
    "signal": 1.0,
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _comp_abs_bbox(comp: Component) -> tuple[float, float, float, float]:
    """Absolute axis-aligned bounding box of a component.

    The schema stores bbox as (x_min, y_min, x_max, y_max) relative to the
    component anchor. No rotation is applied — the bbox is defined as the
    axis-aligned footprint extent regardless of rotation.
    """
    x, y = comp.position
    x0, y0, x1, y1 = comp.bbox
    return (x + x0, y + y0, x + x1, y + y1)


def _cross2d(v1: tuple[float, float], v2: tuple[float, float]) -> float:
    """2D cross product of two vectors."""
    return v1[0] * v2[1] - v1[1] * v2[0]


def _sub(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float]:
    return (p[0] - q[0], p[1] - q[1])


def _segments_intersect(
    seg1: tuple[tuple[float, float], tuple[float, float]],
    seg2: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    """Strict cross-product straddling test for two line segments.

    Returns True only for proper crossings — collinear and touching-endpoint
    cases return False (strict < 0, not <= 0).
    """
    a, b = seg1
    c, d = seg2
    sign1 = _cross2d(_sub(c, a), _sub(b, a)) * _cross2d(_sub(d, a), _sub(b, a))
    sign2 = _cross2d(_sub(a, c), _sub(d, c)) * _cross2d(_sub(b, c), _sub(d, c))
    return sign1 < 0 and sign2 < 0


# ---------------------------------------------------------------------------
# MST ratsnest (Manhattan distance, all connections)
# ---------------------------------------------------------------------------

def _build_mst_edges(
    board: Board,
) -> dict[str, list[tuple[tuple[float, float], tuple[float, float]]]]:
    """Build a minimum spanning tree per net using Manhattan distance.

    NOTE: intentionally diverges from visualizer._compute_ratsnest:
      - Uses Manhattan distance (not Euclidean) as edge weight.
      - Includes ALL pad connections; does NOT exclude already-routed nets.

    Returns {net_name: [(p1, p2), ...]} where each (p1, p2) is an MST edge.
    """
    # Collect absolute pad positions grouped by net
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for comp in board.components.values():
        for pad in comp.pads:
            if not pad.net:
                continue
            pos = comp.pad_abs_position(pad)
            net_pads.setdefault(pad.net, []).append(pos)

    result: dict[str, list[tuple]] = {}

    for net_name, positions in net_pads.items():
        if len(positions) < 2:
            continue

        # Build all edges sorted by Manhattan distance
        edges: list[tuple[float, int, int]] = []
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                dx = abs(positions[i][0] - positions[j][0])
                dy = abs(positions[i][1] - positions[j][1])
                edges.append((dx + dy, i, j))
        edges.sort()

        # Kruskal's MST with path-compressed union-find
        parent = list(range(len(positions)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        mst: list[tuple] = []
        for _, i, j in edges:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
                mst.append((positions[i], positions[j]))

        result[net_name] = mst

    return result


# ---------------------------------------------------------------------------
# Crossing detection
# ---------------------------------------------------------------------------

def _count_crossings(
    mst_edges: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]]
) -> tuple[int, int]:
    """Count pairwise crossings between edges from different nets.

    Returns (crossing_count, n_distinct_net_pairs_tested).
    """
    # Flatten to list of (segment, net_name)
    all_edges: list[tuple[tuple, tuple, str]] = [
        (p1, p2, net)
        for net, edges in mst_edges.items()
        for p1, p2 in edges
    ]

    crossings = 0
    n_pairs = 0
    for i in range(len(all_edges)):
        for j in range(i + 1, len(all_edges)):
            p1, p2, net_i = all_edges[i]
            p3, p4, net_j = all_edges[j]
            if net_i == net_j:
                continue
            n_pairs += 1
            if _segments_intersect((p1, p2), (p3, p4)):
                crossings += 1

    return crossings, n_pairs


# ---------------------------------------------------------------------------
# Wirelength
# ---------------------------------------------------------------------------

def _compute_wirelength(
    mst_edges: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]],
    nets: dict[str, Net],
) -> tuple[float, float]:
    """Return (total_wirelength_mm, weighted_wirelength_mm).

    Manhattan distance per edge. Weighted by net class:
      ground=0 (handled by pour), power=3x, constrained_signal=2x, signal=1x.
    """
    total = 0.0
    weighted = 0.0
    for net_name, edges in mst_edges.items():
        net = nets.get(net_name)
        weight = _NET_CLASS_WEIGHT.get(net.class_ if net else "signal", 1.0)
        for p1, p2 in edges:
            length = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
            total += length
            weighted += length * weight
    return total, weighted


# ---------------------------------------------------------------------------
# Channel capacity
# ---------------------------------------------------------------------------

def _analyze_channels(board: Board) -> dict[str, ChannelInfo]:
    """Analyze routing corridor congestion between adjacent component pairs.

    For each pair of components, checks for:
      - Horizontal corridor: Y-bbox ranges overlap, horizontal gap in (0, 10] mm
      - Vertical corridor: X-bbox ranges overlap, vertical gap in (0, 10] mm

    required_tracks = nets that appear in BOTH components' pad lists.
    available_tracks = floor(gap / (trace_width + min_clearance)).
    """
    track_size = board.rules.default_trace_width_mm + board.rules.min_clearance_mm
    result: dict[str, ChannelInfo] = {}
    comp_items = list(board.components.items())

    for i in range(len(comp_items)):
        for j in range(i + 1, len(comp_items)):
            ref_a, comp_a = comp_items[i]
            ref_b, comp_b = comp_items[j]

            ax0, ay0, ax1, ay1 = _comp_abs_bbox(comp_a)
            bx0, by0, bx1, by1 = _comp_abs_bbox(comp_b)

            nets_a = {pad.net for pad in comp_a.pads if pad.net}
            nets_b = {pad.net for pad in comp_b.pads if pad.net}
            required = len(nets_a & nets_b)

            key_base = f"{min(ref_a, ref_b)}-{max(ref_a, ref_b)}"

            # Horizontal corridor: Y ranges overlap, A left of B or B left of A
            y_overlap = ay0 < by1 and by0 < ay1
            if y_overlap:
                if bx0 >= ax1:
                    h_gap = bx0 - ax1
                elif ax0 >= bx1:
                    h_gap = ax0 - bx1
                else:
                    h_gap = 0.0
                if 0 < h_gap <= 10.0:
                    available = int(math.floor(h_gap / track_size))
                    utilization = required / max(1, available)
                    result[f"{key_base}_h"] = ChannelInfo(
                        component_a=ref_a,
                        component_b=ref_b,
                        available_tracks=available,
                        required_tracks=required,
                        utilization=round(utilization, 4),
                        oversubscribed=(required > available),
                    )

            # Vertical corridor: X ranges overlap, A above B or B above A
            x_overlap = ax0 < bx1 and bx0 < ax1
            if x_overlap:
                if by0 >= ay1:
                    v_gap = by0 - ay1
                elif ay0 >= by1:
                    v_gap = ay0 - by1
                else:
                    v_gap = 0.0
                if 0 < v_gap <= 10.0:
                    available = int(math.floor(v_gap / track_size))
                    utilization = required / max(1, available)
                    result[f"{key_base}_v"] = ChannelInfo(
                        component_a=ref_a,
                        component_b=ref_b,
                        available_tracks=available,
                        required_tracks=required,
                        utilization=round(utilization, 4),
                        oversubscribed=(required > available),
                    )

    return result


# ---------------------------------------------------------------------------
# Pin escape analysis
# ---------------------------------------------------------------------------

def _check_pin_escape(board: Board) -> list[str]:
    """Return list of 'REF:pad_num' for pads with ≥3/4 cardinal directions blocked.

    A direction is blocked if the probe point (pad position ± one grid step)
    lands outside the board bounding box or inside another component's bbox.
    """
    grid = board.grid_step
    outline_xs = [pt[0] for pt in board.board_outline]
    outline_ys = [pt[1] for pt in board.board_outline]
    board_min_x, board_max_x = min(outline_xs), max(outline_xs)
    board_min_y, board_max_y = min(outline_ys), max(outline_ys)

    comp_bboxes = {ref: _comp_abs_bbox(c) for ref, c in board.components.items()}

    violations: list[str] = []
    for ref, comp in board.components.items():
        for pad in comp.pads:
            px, py = comp.pad_abs_position(pad)
            blocked = 0
            for dx, dy in [(grid, 0.0), (-grid, 0.0), (0.0, grid), (0.0, -grid)]:
                probe_x = px + dx
                probe_y = py + dy
                # Outside board bounds
                if (probe_x < board_min_x or probe_x > board_max_x
                        or probe_y < board_min_y or probe_y > board_max_y):
                    blocked += 1
                    continue
                # Inside another component's bbox
                for other_ref, (ox0, oy0, ox1, oy1) in comp_bboxes.items():
                    if other_ref == ref:
                        continue
                    if ox0 <= probe_x <= ox1 and oy0 <= probe_y <= oy1:
                        blocked += 1
                        break
            if blocked >= 3:
                violations.append(f"{ref}:{pad.number}")

    return violations


# ---------------------------------------------------------------------------
# Board geometry helpers
# ---------------------------------------------------------------------------

def _board_diagonal(board: Board) -> float:
    """Bounding-box diagonal of the board outline."""
    xs = [pt[0] for pt in board.board_outline]
    ys = [pt[1] for pt in board.board_outline]
    return math.sqrt((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2)


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def _compute_composite(
    crossings: int,
    n_pairs: int,
    total_wl: float,
    n_connections: int,
    diagonal: float,
    channels: dict[str, ChannelInfo],
    n_violations: int,
    total_pads: int,
    n_constraint_violations: int = 0,
) -> float:
    """Compute the 0–100 composite placement score.

    Sub-scores (all 0–1, higher is better):
      - s_cross:  1 - crossings / n_pairs
      - s_wl:     1 - total_wl / (n_connections * board_diagonal)
      - s_chan:   mean(1 - utilization) across corridors, 1.0 if no corridors
      - s_esc:    1 - n_violations / total_pads

    Weights: crossings=25%, wirelength=25%, channel=20%, escapes=15%, constraints=15%.
    Constraint violations apply a hard penalty: each violation costs 10 points
    (capped at 50), applied after the weighted sum.
    """
    s_cross = 1.0 - crossings / max(1, n_pairs)

    ref_wl = n_connections * diagonal
    s_wl = max(0.0, min(1.0, 1.0 - total_wl / max(1.0, ref_wl)))

    if channels:
        channel_scores = [max(0.0, 1.0 - ci.utilization) for ci in channels.values()]
        s_chan = sum(channel_scores) / len(channel_scores)
    else:
        s_chan = 1.0

    s_esc = max(0.0, min(1.0, 1.0 - n_violations / max(1, total_pads)))

    base = (0.25 * s_cross + 0.25 * s_wl + 0.20 * s_chan + 0.15 * s_esc) * 100
    # Constraint penalty: hard deductions that override optimisation
    constraint_penalty = min(50.0, n_constraint_violations * 10.0)
    composite = max(0.0, base - constraint_penalty)
    return round(composite, 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_placement(board: Board) -> PlacementScore:
    """Compute placement quality metrics for a board."""
    from src.apply_constraints import check_constraint_violations
    mst = _build_mst_edges(board)
    crossings, n_pairs = _count_crossings(mst)
    total_wl, weighted_wl = _compute_wirelength(mst, board.nets)
    channels = _analyze_channels(board)
    violations = _check_pin_escape(board)
    constraint_violations = check_constraint_violations(board)

    n_connections = sum(len(edges) for edges in mst.values())
    total_pads = sum(len(c.pads) for c in board.components.values())
    diagonal = _board_diagonal(board)

    composite = _compute_composite(
        crossings, n_pairs, total_wl, n_connections,
        diagonal, channels, len(violations), total_pads,
        n_constraint_violations=len(constraint_violations),
    )

    return PlacementScore(
        ratsnest_crossings=crossings,
        total_wirelength_mm=round(total_wl, 4),
        weighted_wirelength_mm=round(weighted_wl, 4),
        channel_capacity=channels,
        pin_escape_violations=violations,
        constraint_violations=constraint_violations,
        composite_score=composite,
    )


def score_to_dict(score: PlacementScore) -> dict:
    """Serialize PlacementScore to a JSON-compatible dict."""
    return {
        "ratsnest_crossings": score.ratsnest_crossings,
        "total_wirelength_mm": score.total_wirelength_mm,
        "weighted_wirelength_mm": score.weighted_wirelength_mm,
        "channel_capacity": {
            k: {
                "component_a": ci.component_a,
                "component_b": ci.component_b,
                "available_tracks": ci.available_tracks,
                "required_tracks": ci.required_tracks,
                "utilization": ci.utilization,
                "oversubscribed": ci.oversubscribed,
            }
            for k, ci in score.channel_capacity.items()
        },
        "pin_escape_violations": score.pin_escape_violations,
        "constraint_violations": score.constraint_violations,
        "composite_score": score.composite_score,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Score PCB component placement quality")
    parser.add_argument("input", help="Input board.json file")
    args = parser.parse_args()

    board = load_board(args.input)
    score = score_placement(board)
    print(json.dumps(score_to_dict(score), indent=2))


if __name__ == "__main__":
    main()
