"""Board spatial analysis for agent-driven routing decisions.

Provides structured data about pad copper extents, passable gaps between
components, routing corridors, and per-net obstacle summaries. The agent
reads this output to plan waypoint routes without hitting obstacles.

CLI:
    python -m src.board_analyzer board.json              # full JSON
    python -m src.board_analyzer board.json --net "+3.3V" # single net
    python -m src.board_analyzer board.json --gaps        # gaps only
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.schema import Board, Component, Pad, load_board
from src.placement_scorer import _build_mst_edges, _comp_abs_bbox


# ---------------------------------------------------------------------------
# Pad copper extent computation
# ---------------------------------------------------------------------------

def _pad_copper_rect(
    comp: Component, pad: Pad,
) -> tuple[float, float, float, float]:
    """Return (x_min, y_min, x_max, y_max) of a pad's copper area in board coords.

    Accounts for component rotation. KiCad uses clockwise-positive rotation.
    """
    cx, cy = comp.pad_abs_position(pad)
    # Pad size is in footprint-local space. At rotation, width/height may swap.
    rad = math.radians(-comp.rotation)
    cos_r, sin_r = abs(math.cos(rad)), abs(math.sin(rad))
    # Effective half-extents after rotation
    hw = pad.size[0] / 2 * cos_r + pad.size[1] / 2 * sin_r
    hh = pad.size[0] / 2 * sin_r + pad.size[1] / 2 * cos_r
    return (cx - hw, cy - hh, cx + hw, cy + hh)


def _is_routable_net(net_name: str) -> bool:
    return bool(net_name) and not net_name.startswith("unconnected")


# ---------------------------------------------------------------------------
# Component obstacle analysis
# ---------------------------------------------------------------------------

@dataclass
class PadZone:
    ref: str
    pad_number: str
    net: str
    center: tuple[float, float]
    copper_rect: tuple[float, float, float, float]  # x_min, y_min, x_max, y_max
    layer: str


def _analyze_component(comp: Component) -> dict:
    """Analyze a single component's obstacle footprint."""
    is_smd = not any(p.layer == "*.Cu" for p in comp.pads)
    pad_zones = []
    for pad in comp.pads:
        cx, cy = comp.pad_abs_position(pad)
        rect = _pad_copper_rect(comp, pad)
        pad_zones.append({
            "pad": pad.number,
            "net": pad.net,
            "center": [round(cx, 3), round(cy, 3)],
            "copper_rect": [round(v, 3) for v in rect],
            "layer": pad.layer,
        })

    result: dict = {
        "type": "SMD" if is_smd else "THT",
        "center": list(comp.position),
        "rotation": comp.rotation,
        "layer": comp.layer,
        "footprint": comp.footprint.split(":")[-1],
        "pads": pad_zones,
    }

    # For SMD: detect pad rows and center gap
    if is_smd and len(comp.pads) >= 4:
        # Group pads by approximate Y coordinate (within 0.5mm)
        y_groups: dict[float, list[dict]] = {}
        for pz in pad_zones:
            y = round(pz["center"][1] * 2) / 2  # round to 0.5mm
            y_groups.setdefault(y, []).append(pz)

        if len(y_groups) >= 2:
            sorted_ys = sorted(y_groups.keys())
            rows = []
            for y_key in sorted_ys:
                pads_in_row = y_groups[y_key]
                all_rects = [p["copper_rect"] for p in pads_in_row]
                row_x_min = min(r[0] for r in all_rects)
                row_x_max = max(r[2] for r in all_rects)
                row_y_min = min(r[1] for r in all_rects)
                row_y_max = max(r[3] for r in all_rects)
                rows.append({
                    "y": round(y_key, 3),
                    "pad_count": len(pads_in_row),
                    "x_range": [round(row_x_min, 3), round(row_x_max, 3)],
                    "copper_y_range": [round(row_y_min, 3), round(row_y_max, 3)],
                })
            result["pad_rows"] = rows

            # Center gap between first and last row
            if len(rows) >= 2:
                gap_y_min = rows[0]["copper_y_range"][1]
                gap_y_max = rows[-1]["copper_y_range"][0]
                gap_x_min = min(r["x_range"][0] for r in rows)
                gap_x_max = max(r["x_range"][1] for r in rows)
                gap_height = gap_y_max - gap_y_min
                result["center_gap"] = {
                    "y_range": [round(gap_y_min, 3), round(gap_y_max, 3)],
                    "x_range": [round(gap_x_min, 3), round(gap_x_max, 3)],
                    "height_mm": round(gap_height, 3),
                    "passable": gap_height >= 0.5,
                    "max_trace_width_mm": round(max(0, gap_height - 0.4), 2),
                }

    return result


# ---------------------------------------------------------------------------
# Gap detection between adjacent components
# ---------------------------------------------------------------------------

def _find_gaps(board: Board, min_width_mm: float = 0.3) -> list[dict]:
    """Find passable gaps between adjacent component pad zones.

    Returns gaps with coordinates, width, and max passable trace width.
    """
    clearance_mm = 0.2  # KiCad default clearance

    # Collect all pad copper rects with component info
    all_zones: list[tuple[str, tuple[float, float, float, float], str]] = []
    for ref, comp in board.components.items():
        for pad in comp.pads:
            rect = _pad_copper_rect(comp, pad)
            all_zones.append((ref, rect, pad.layer))

    gaps: list[dict] = []

    # Check all component pairs for gaps
    refs = list(board.components.keys())
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            ref_a, ref_b = refs[i], refs[j]
            comp_a = board.components[ref_a]
            comp_b = board.components[ref_b]

            # Get all pad rects for each component
            rects_a = [_pad_copper_rect(comp_a, p) for p in comp_a.pads]
            rects_b = [_pad_copper_rect(comp_b, p) for p in comp_b.pads]

            if not rects_a or not rects_b:
                continue

            # Bounding box of each component's pads
            a_xmin = min(r[0] for r in rects_a)
            a_xmax = max(r[2] for r in rects_a)
            a_ymin = min(r[1] for r in rects_a)
            a_ymax = max(r[3] for r in rects_a)

            b_xmin = min(r[0] for r in rects_b)
            b_xmax = max(r[2] for r in rects_b)
            b_ymin = min(r[1] for r in rects_b)
            b_ymax = max(r[3] for r in rects_b)

            # Check for vertical gap (components side by side in X)
            if a_xmax < b_xmin and abs((a_ymin + a_ymax) / 2 - (b_ymin + b_ymax) / 2) < max(a_ymax - a_ymin, b_ymax - b_ymin):
                gap_width = b_xmin - a_xmax
                if gap_width >= min_width_mm:
                    gap_x = (a_xmax + b_xmin) / 2
                    gap_y_min = max(a_ymin, b_ymin)
                    gap_y_max = min(a_ymax, b_ymax)
                    if gap_y_max > gap_y_min:
                        max_trace = max(0, gap_width - 2 * clearance_mm)
                        gaps.append({
                            "between": [ref_a, ref_b],
                            "direction": "vertical",
                            "center_x": round(gap_x, 3),
                            "x_range": [round(a_xmax, 3), round(b_xmin, 3)],
                            "y_range": [round(gap_y_min, 3), round(gap_y_max, 3)],
                            "width_mm": round(gap_width, 3),
                            "max_trace_width_mm": round(max_trace, 2),
                        })

            elif b_xmax < a_xmin and abs((a_ymin + a_ymax) / 2 - (b_ymin + b_ymax) / 2) < max(a_ymax - a_ymin, b_ymax - b_ymin):
                gap_width = a_xmin - b_xmax
                if gap_width >= min_width_mm:
                    gap_x = (b_xmax + a_xmin) / 2
                    gap_y_min = max(a_ymin, b_ymin)
                    gap_y_max = min(a_ymax, b_ymax)
                    if gap_y_max > gap_y_min:
                        max_trace = max(0, gap_width - 2 * clearance_mm)
                        gaps.append({
                            "between": [ref_b, ref_a],
                            "direction": "vertical",
                            "center_x": round(gap_x, 3),
                            "x_range": [round(b_xmax, 3), round(a_xmin, 3)],
                            "y_range": [round(gap_y_min, 3), round(gap_y_max, 3)],
                            "width_mm": round(gap_width, 3),
                            "max_trace_width_mm": round(max_trace, 2),
                        })

            # Check for horizontal gap (components stacked in Y)
            if a_ymax < b_ymin and abs((a_xmin + a_xmax) / 2 - (b_xmin + b_xmax) / 2) < max(a_xmax - a_xmin, b_xmax - b_xmin):
                gap_height = b_ymin - a_ymax
                if gap_height >= min_width_mm:
                    gap_y = (a_ymax + b_ymin) / 2
                    gap_x_min = max(a_xmin, b_xmin)
                    gap_x_max = min(a_xmax, b_xmax)
                    if gap_x_max > gap_x_min:
                        max_trace = max(0, gap_height - 2 * clearance_mm)
                        gaps.append({
                            "between": [ref_a, ref_b],
                            "direction": "horizontal",
                            "center_y": round(gap_y, 3),
                            "y_range": [round(a_ymax, 3), round(b_ymin, 3)],
                            "x_range": [round(gap_x_min, 3), round(gap_x_max, 3)],
                            "width_mm": round(gap_height, 3),
                            "max_trace_width_mm": round(max_trace, 2),
                        })

            elif b_ymax < a_ymin and abs((a_xmin + a_xmax) / 2 - (b_xmin + b_xmax) / 2) < max(a_xmax - a_xmin, b_xmax - b_xmin):
                gap_height = a_ymin - b_ymax
                if gap_height >= min_width_mm:
                    gap_y = (b_ymax + a_ymin) / 2
                    gap_x_min = max(a_xmin, b_xmin)
                    gap_x_max = min(a_xmax, b_xmax)
                    if gap_x_max > gap_x_min:
                        max_trace = max(0, gap_height - 2 * clearance_mm)
                        gaps.append({
                            "between": [ref_b, ref_a],
                            "direction": "horizontal",
                            "center_y": round(gap_y, 3),
                            "y_range": [round(b_ymax, 3), round(a_ymin, 3)],
                            "x_range": [round(gap_x_min, 3), round(gap_x_max, 3)],
                            "width_mm": round(gap_height, 3),
                            "max_trace_width_mm": round(max_trace, 2),
                        })

    return gaps


# ---------------------------------------------------------------------------
# Net routing analysis
# ---------------------------------------------------------------------------

def _analyze_net(board: Board, net_name: str, all_gaps: list[dict]) -> dict:
    """Analyze a single net's routing requirements."""
    net = board.nets.get(net_name)
    if not net:
        return {}

    pads: list[dict] = []
    for ref, comp in board.components.items():
        for pad in comp.pads:
            if pad.net != net_name:
                continue
            cx, cy = comp.pad_abs_position(pad)
            pads.append({
                "ref": f"{ref}.{pad.number}",
                "pos": [round(cx, 3), round(cy, 3)],
                "layer": pad.layer,
            })

    if len(pads) < 2:
        return {}

    # Bounding box of all pads
    all_x = [p["pos"][0] for p in pads]
    all_y = [p["pos"][1] for p in pads]

    # Classify connections by distance
    mst = _build_mst_edges(board)
    edges = mst.get(net_name, [])
    short_connections = []
    long_connections = []
    for p1, p2 in edges:
        dist = abs(p1[0] - p2[0]) + abs(p1[1] - p2[1])
        # Find pad refs
        ref1 = next((p["ref"] for p in pads if abs(p["pos"][0] - p1[0]) < 0.1 and abs(p["pos"][1] - p1[1]) < 0.1), "?")
        ref2 = next((p["ref"] for p in pads if abs(p["pos"][0] - p2[0]) < 0.1 and abs(p["pos"][1] - p2[1]) < 0.1), "?")
        entry = [ref1, ref2, round(dist, 1)]
        if dist < 5.0:
            short_connections.append(entry)
        else:
            long_connections.append(entry)

    # Find relevant gaps (gaps whose spatial extent overlaps the net's bounding box)
    relevant_gaps = []
    net_bbox = (min(all_x) - 2, min(all_y) - 2, max(all_x) + 2, max(all_y) + 2)
    for gap in all_gaps:
        gx = gap.get("x_range", [0, 0])
        gy = gap.get("y_range", [0, 0])
        if gx[1] > net_bbox[0] and gx[0] < net_bbox[2] and gy[1] > net_bbox[1] and gy[0] < net_bbox[3]:
            relevant_gaps.append(f"{gap['between'][0]}↔{gap['between'][1]} ({gap['direction']}, {gap['width_mm']:.1f}mm)")

    return {
        "class": net.class_,
        "width_mm": net.width_mm,
        "pad_count": len(pads),
        "pads": pads,
        "bounding_box": [round(min(all_x), 2), round(min(all_y), 2),
                         round(max(all_x), 2), round(max(all_y), 2)],
        "short_connections": short_connections,
        "long_connections": long_connections,
        "nearby_gaps": relevant_gaps,
    }


# ---------------------------------------------------------------------------
# Full board analysis
# ---------------------------------------------------------------------------

def analyze_board(board: Board, net_filter: Optional[str] = None) -> dict:
    """Run full spatial analysis on the board."""
    outline_xs = [pt[0] for pt in board.board_outline]
    outline_ys = [pt[1] for pt in board.board_outline]

    # Component analysis
    components = {}
    for ref, comp in board.components.items():
        components[ref] = _analyze_component(comp)

    # Gap detection
    gaps = _find_gaps(board)

    # Net analysis
    nets = {}
    for name, net in board.nets.items():
        if net.class_ == "ground" or name.startswith("unconnected"):
            continue
        if net_filter and name != net_filter:
            continue
        analysis = _analyze_net(board, name, gaps)
        if analysis:
            nets[name] = analysis

    return {
        "board_size": {
            "width": round(max(outline_xs) - min(outline_xs), 1),
            "height": round(max(outline_ys) - min(outline_ys), 1),
        },
        "components": components,
        "gaps": gaps,
        "nets": nets,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Board spatial analysis for routing")
    parser.add_argument("board_json", help="Input board.json")
    parser.add_argument("--net", default=None, help="Analyze single net")
    parser.add_argument("--gaps", action="store_true", help="Show gaps only")
    parser.add_argument("--text", action="store_true", help="Human-readable output")
    args = parser.parse_args()

    board = load_board(args.board_json)
    result = analyze_board(board, net_filter=args.net)

    if args.gaps:
        result = {"gaps": result["gaps"]}

    if args.text:
        print(f"Board: {result.get('board_size', {}).get('width', '?')} x {result.get('board_size', {}).get('height', '?')} mm")
        print()

        if "gaps" in result and result["gaps"]:
            print(f"=== Passable Gaps ({len(result['gaps'])}) ===")
            for gap in result["gaps"]:
                between = f"{gap['between'][0]}↔{gap['between'][1]}"
                print(f"  {between}: {gap['direction']} {gap['width_mm']:.1f}mm "
                      f"(max trace {gap['max_trace_width_mm']:.1f}mm)")

        if "nets" in result:
            print(f"\n=== Nets ({len(result['nets'])}) ===")
            for name, net in sorted(result["nets"].items()):
                print(f"  {name} ({net['class']}, {net['pad_count']} pads)")
                for conn in net.get("long_connections", []):
                    print(f"    LONG: {conn[0]} → {conn[1]} ({conn[2]}mm)")
                for conn in net.get("short_connections", []):
                    print(f"    short: {conn[0]} → {conn[1]} ({conn[2]}mm)")

        if "components" in result:
            print(f"\n=== Components ({len(result['components'])}) ===")
            for ref, comp in sorted(result["components"].items()):
                cg = comp.get("center_gap")
                if cg:
                    print(f"  {ref}: {comp['type']} — center gap {cg['height_mm']:.1f}mm "
                          f"(max trace {cg['max_trace_width_mm']:.1f}mm)")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
