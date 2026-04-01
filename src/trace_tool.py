"""Point-to-point waypoint trace tool.

Converts a list of waypoints into Segments and Vias in board.json.
No grid, no A*, no occupancy grid — the agent decides WHERE to route,
this tool just draws the segments.

CLI:
    python -m src.trace_tool board.json --plan '{"net":"+3.3V","waypoints":[[22.5,25.6],[22.5,45.0]],"layer":"F.Cu"}'
    python -m src.trace_tool board.json --plan route_plan.json -o board.json
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.schema import Board, Net, Route, Segment, Via, load_board, save_board


# ---------------------------------------------------------------------------
# Route plan
# ---------------------------------------------------------------------------

@dataclass
class RoutePlan:
    net: str                                    # net name
    waypoints: list[tuple[float, float]]        # ordered board coordinates
    layer: str = "F.Cu"                         # starting layer
    width_mm: Optional[float] = None            # None = use net default
    vias: list[int] = field(default_factory=list)  # waypoint indices where layer changes


LAYERS = ["F.Cu", "B.Cu"]


def _plan_from_dict(d: dict) -> RoutePlan:
    return RoutePlan(
        net=d["net"],
        waypoints=[tuple(wp) for wp in d["waypoints"]],
        layer=d.get("layer", "F.Cu"),
        width_mm=d.get("width_mm"),
        vias=d.get("vias", []),
    )


# ---------------------------------------------------------------------------
# Pad snapping
# ---------------------------------------------------------------------------

def _build_net_pad_map(board: Board) -> dict[str, list[tuple[float, float]]]:
    """Return {net_name: [(x, y), ...]} for all pads on the board."""
    result: dict[str, list[tuple[float, float]]] = {}
    for comp in board.components.values():
        for pad in comp.pads:
            if pad.net:
                pos = comp.pad_abs_position(pad)
                result.setdefault(pad.net, []).append(pos)
    return result


def _snap_to_pad(
    x: float, y: float, net_name: str,
    pad_map: dict[str, list[tuple[float, float]]],
    threshold: float = 1.0,
) -> tuple[float, float]:
    """Snap (x, y) to the nearest pad on the same net if within threshold."""
    best_dist = threshold + 1
    best_pos = (x, y)
    for px, py in pad_map.get(net_name, []):
        dist = math.sqrt((x - px) ** 2 + (y - py) ** 2)
        if dist < best_dist:
            best_dist = dist
            best_pos = (px, py)
    return best_pos if best_dist <= threshold else (x, y)


# ---------------------------------------------------------------------------
# Clearance check (advisory)
# ---------------------------------------------------------------------------

def _segments_intersect_2d(
    a1: tuple[float, float], a2: tuple[float, float],
    b1: tuple[float, float], b2: tuple[float, float],
) -> bool:
    """Check if two line segments intersect (2D, ignoring endpoints touching)."""
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1 = cross(b1, b2, a1)
    d2 = cross(b1, b2, a2)
    d3 = cross(a1, a2, b1)
    d4 = cross(a1, a2, b2)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def check_clearance(board: Board, new_segments: list[Segment], net_name: str) -> list[str]:
    """Return warnings for new segments that intersect existing routes on the same layer."""
    warnings: list[str] = []
    for route in board.routes:
        if route.net == net_name:
            continue  # same net is fine
        for existing_seg in route.segments:
            for new_seg in new_segments:
                if existing_seg.layer != new_seg.layer:
                    continue
                if _segments_intersect_2d(
                    existing_seg.start, existing_seg.end,
                    new_seg.start, new_seg.end
                ):
                    warnings.append(
                        f"Warning: {net_name} crosses {route.net} on {new_seg.layer} "
                        f"near ({new_seg.start[0]:.1f},{new_seg.start[1]:.1f})"
                    )
    return warnings


# ---------------------------------------------------------------------------
# Core: waypoints → segments + vias
# ---------------------------------------------------------------------------

def plan_to_route(plan: RoutePlan, board: Board) -> tuple[Route, list[Via]]:
    """Convert a RoutePlan to a Route and list of Vias.

    Snaps first/last waypoints to actual pad positions.
    """
    if len(plan.waypoints) < 2:
        raise ValueError(f"Route plan for {plan.net} needs at least 2 waypoints")

    net = board.nets.get(plan.net)
    if net is None:
        raise ValueError(f"Unknown net: {plan.net}")

    # Determine trace width
    width = plan.width_mm
    if width is None:
        width = net.width_mm or board.rules.default_trace_width_mm

    # Snap endpoints to pads
    pad_map = _build_net_pad_map(board)
    waypoints = list(plan.waypoints)
    waypoints[0] = _snap_to_pad(waypoints[0][0], waypoints[0][1], plan.net, pad_map)
    waypoints[-1] = _snap_to_pad(waypoints[-1][0], waypoints[-1][1], plan.net, pad_map)

    # Build segments and vias
    via_set = set(plan.vias)
    current_layer = plan.layer
    segments: list[Segment] = []
    vias: list[Via] = []

    for i in range(len(waypoints) - 1):
        p1 = waypoints[i]
        p2 = waypoints[i + 1]

        # If this waypoint is a via point, place via and switch layers
        if i in via_set:
            vias.append(Via(
                position=p1,
                net=plan.net,
                drill_mm=board.rules.via_drill_mm,
            ))
            current_layer = LAYERS[1 - LAYERS.index(current_layer)]

        segments.append(Segment(start=p1, end=p2, layer=current_layer))

    # Handle via at the last waypoint (if specified)
    if (len(waypoints) - 1) in via_set:
        vias.append(Via(
            position=waypoints[-1],
            net=plan.net,
            drill_mm=board.rules.via_drill_mm,
        ))

    route = Route(net=plan.net, width_mm=width, segments=segments)
    return route, vias


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_route(board: Board, plan: RoutePlan) -> tuple[Board, list[str]]:
    """Add a route to the board from a RoutePlan.

    Returns (updated_board, warnings).
    Warnings are advisory clearance issues — the agent decides what to do.
    """
    route, vias = plan_to_route(plan, board)

    # Check clearance against existing routes
    warnings = check_clearance(board, route.segments, plan.net)

    # Add to board
    new_routes = list(board.routes) + [route]
    new_vias = list(board.vias) + vias

    updated = Board(
        board_outline=board.board_outline,
        grid_step=board.grid_step,
        rules=board.rules,
        components=board.components,
        nets=board.nets,
        keepouts=board.keepouts,
        routes=new_routes,
        vias=new_vias,
        pours=board.pours,
    )
    return updated, warnings


def remove_net_routes(board: Board, net_name: str) -> Board:
    """Remove all routes and routing vias for a specific net.

    GND vias (via-in-pad) are preserved.
    """
    new_routes = [r for r in board.routes if r.net != net_name]
    new_vias = [v for v in board.vias if v.net != net_name]
    # Preserve GND vias even if we're clearing GND routes
    if net_name in ("GND", "AGND", "DGND", "PGND"):
        new_vias = list(board.vias)  # keep all vias for ground nets

    return Board(
        board_outline=board.board_outline,
        grid_step=board.grid_step,
        rules=board.rules,
        components=board.components,
        nets=board.nets,
        keepouts=board.keepouts,
        routes=new_routes,
        vias=new_vias,
        pours=board.pours,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Add a waypoint route to board.json")
    parser.add_argument("board_json", help="Input board.json")
    parser.add_argument("--plan", required=True,
                        help="Route plan: inline JSON string or path to .json file")
    parser.add_argument("-o", "--output", help="Output board.json (default: overwrite input)")
    parser.add_argument("--remove-net", help="Remove all routes for this net before adding")
    args = parser.parse_args()

    board = load_board(args.board_json)
    output_path = args.output or args.board_json

    # Remove existing routes for the net if requested
    if args.remove_net:
        board = remove_net_routes(board, args.remove_net)

    # Parse plan
    plan_str = args.plan.strip()
    if plan_str.startswith("{"):
        plan_dict = json.loads(plan_str)
    else:
        with open(plan_str) as f:
            plan_dict = json.load(f)

    plan = _plan_from_dict(plan_dict)

    # Add route
    board, warnings = add_route(board, plan)

    for w in warnings:
        print(w)

    save_board(board, output_path)
    n_segs = len(board.routes[-1].segments)
    n_vias = len(plan.vias)
    print(f"Added {plan.net}: {n_segs} segments, {n_vias} vias → {output_path}")


if __name__ == "__main__":
    main()
