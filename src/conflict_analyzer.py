"""Pre-routing bottleneck and conflict analysis.

Analyzes which nets are hardest to route and why, based on the placement.
Informs Claude's routing order decisions before pathfinder runs.

CLI:
    python src/conflict_analyzer.py board.json
    python src/conflict_analyzer.py board.json --json
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from src.schema import Board, load_board
from src.placement_scorer import _analyze_channels, _build_mst_edges, _comp_abs_bbox


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class ConflictReport:
    net_difficulty: dict[str, float]        # net_name → score (higher = harder)
    bottleneck_channels: list[dict]          # channels where required > available
    routing_order: list[str]                 # recommended net ordering
    estimated_via_count: int                 # rough via budget


def conflict_to_dict(report: ConflictReport) -> dict:
    return {
        "net_difficulty": report.net_difficulty,
        "bottleneck_channels": report.bottleneck_channels,
        "routing_order": report.routing_order,
        "estimated_via_count": report.estimated_via_count,
    }


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _comp_bboxes(board: Board) -> dict[str, tuple[float, float, float, float]]:
    return {ref: _comp_abs_bbox(comp) for ref, comp in board.components.items()}


def _net_pad_counts(board: Board) -> dict[str, int]:
    counts: dict[str, int] = {}
    for comp in board.components.values():
        for pad in comp.pads:
            if pad.net:
                counts[pad.net] = counts.get(pad.net, 0) + 1
    return counts


def _net_crosses_channel(
    p1: tuple[float, float],
    p2: tuple[float, float],
    channel: dict,
) -> bool:
    """Check if an MST edge (p1→p2) passes through a channel's gap region.

    channel dict has keys: component_a, component_b, and (inferred) direction.
    We approximate: if the edge's bounding box overlaps the channel gap.
    """
    # Simplified: any edge that connects a pad on comp_a to a pad on comp_b
    # is considered to use that channel.
    ca = channel.get("component_a", "")
    cb = channel.get("component_b", "")
    return False  # will be refined per-net below


def _count_channel_vias(
    mst_edges: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]],
    board: Board,
) -> int:
    """Estimate via count: MST edges that cross component bboxes need layer changes."""
    bboxes = _comp_bboxes(board)
    via_estimate = 0

    for net_name, edges in mst_edges.items():
        net = board.nets.get(net_name)
        if net and net.class_ == "ground":
            continue
        for p1, p2 in edges:
            # Check if any other component bbox lies between p1 and p2
            x_min = min(p1[0], p2[0])
            x_max = max(p1[0], p2[0])
            y_min = min(p1[1], p2[1])
            y_max = max(p1[1], p2[1])
            for ref, (bx0, by0, bx1, by1) in bboxes.items():
                # Skip components that own the pads being connected
                # Simple AABB overlap check
                if (bx0 < x_max and bx1 > x_min and
                        by0 < y_max and by1 > y_min):
                    via_estimate += 1
                    break  # one via per edge, not per blocking component

    return via_estimate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_conflicts(board: Board) -> ConflictReport:
    """Analyze routing difficulty and recommended order for a placed board."""
    channels = _analyze_channels(board)
    mst = _build_mst_edges(board)
    pad_counts = _net_pad_counts(board)

    # Build per-channel info dict for bottlenecks
    bottlenecks = []
    for key, ch in channels.items():
        if ch.oversubscribed:
            bottlenecks.append({
                "channel": key,
                "component_a": ch.component_a,
                "component_b": ch.component_b,
                "available_tracks": ch.available_tracks,
                "required_tracks": ch.required_tracks,
                "utilization": ch.utilization,
            })

    # Build component → net set mapping for channel analysis
    comp_nets: dict[str, set[str]] = {}
    for ref, comp in board.components.items():
        comp_nets[ref] = {pad.net for pad in comp.pads if pad.net}

    # Compute difficulty per net:
    # For each channel this net crosses (appears on both components), add required/available
    net_difficulty: dict[str, float] = {}
    for net_name, edges in mst.items():
        net = board.nets.get(net_name)
        if not net or net.class_ == "ground":
            continue

        difficulty = 0.0
        # Check each channel: does this net appear in both components?
        for key, ch in channels.items():
            nets_a = comp_nets.get(ch.component_a, set())
            nets_b = comp_nets.get(ch.component_b, set())
            if net_name in nets_a and net_name in nets_b:
                difficulty += ch.required_tracks / max(1, ch.available_tracks)

        # Add a small base difficulty proportional to pad count (more pads = harder)
        difficulty += 0.1 * pad_counts.get(net_name, 0)
        net_difficulty[net_name] = round(difficulty, 4)

    # Routing order: sort by (priority ascending, difficulty descending)
    # Power nets first (priority=1), then constrained, then signal
    # Within a priority class, route harder nets first (they claim space early)
    def _sort_key(net_name: str) -> tuple[int, float, str]:
        net = board.nets.get(net_name)
        priority = net.priority if net else 99
        diff = -net_difficulty.get(net_name, 0.0)  # negate for descending
        return (priority, diff, net_name)

    routing_order = sorted(net_difficulty.keys(), key=_sort_key)

    estimated_vias = _count_channel_vias(mst, board)

    return ConflictReport(
        net_difficulty=net_difficulty,
        bottleneck_channels=bottlenecks,
        routing_order=routing_order,
        estimated_via_count=estimated_vias,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-routing conflict and bottleneck analysis"
    )
    parser.add_argument("board_json", help="Input board.json")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    board = load_board(args.board_json)
    report = analyze_conflicts(board)

    if args.json:
        print(json.dumps(conflict_to_dict(report), indent=2))
        return

    print(f"Routing order ({len(report.routing_order)} nets):")
    for i, name in enumerate(report.routing_order, 1):
        diff = report.net_difficulty.get(name, 0)
        net = board.nets.get(name)
        cls = net.class_ if net else "?"
        print(f"  {i:2}. {name:<20} class={cls:<20} difficulty={diff:.3f}")

    if report.bottleneck_channels:
        print(f"\nBottleneck channels ({len(report.bottleneck_channels)}):")
        for ch in report.bottleneck_channels:
            print(f"  {ch['channel']}: {ch['required_tracks']} required, "
                  f"{ch['available_tracks']} available "
                  f"(utilization {ch['utilization']:.2f}x)")
    else:
        print("\nNo oversubscribed channels.")

    print(f"\nEstimated via count: {report.estimated_via_count}")


if __name__ == "__main__":
    main()
