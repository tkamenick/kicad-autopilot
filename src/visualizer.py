"""Board state visualizer: board.json → SVG.

Renders board outline, component bboxes, pads (colored by net),
ratsnest (MST per net), routes, vias, and keepouts.

CLI:
    python src/visualizer.py board.json -o board.svg
    python src/visualizer.py board.json -o board.svg --show-ratsnest --show-routes
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.schema import Board, Component, Net, Pad, Route, Via, load_board


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RenderOptions:
    scale: float = 10.0            # px per mm
    show_outline: bool = True
    show_components: bool = True
    show_pads: bool = True
    show_ratsnest: bool = True
    show_routes: bool = True
    show_vias: bool = True
    show_keepouts: bool = True
    show_labels: bool = True
    pad_color_by_net: bool = True
    margin_px: float = 20.0


# ---------------------------------------------------------------------------
# Color assignment
# ---------------------------------------------------------------------------

# Fixed colors for special net classes
_NET_CLASS_COLORS: dict[str, str] = {
    "ground": "#222222",
    "power": "#cc2222",
}

# HSL hue cycle for signal nets (evenly spaced, high saturation)
_SIGNAL_HUES = [210, 120, 40, 270, 170, 310, 60, 0, 90, 330]


def _net_color(net_name: str, class_: str, index: int) -> str:
    """Return a hex color for a net."""
    if class_ in _NET_CLASS_COLORS:
        return _NET_CLASS_COLORS[class_]
    hue = _SIGNAL_HUES[index % len(_SIGNAL_HUES)]
    return f"hsl({hue}, 80%, 45%)"


def _layer_color(layer: str) -> str:
    if layer == "F.Cu":
        return "#cc4444"
    if layer == "B.Cu":
        return "#4444cc"
    return "#888888"


# ---------------------------------------------------------------------------
# Ratsnest (MST per net via Kruskal's)
# ---------------------------------------------------------------------------

def _compute_ratsnest(board: Board) -> dict[str, list[tuple[tuple, tuple]]]:
    """Return MST edges per net: {net_name: [(p1, p2), ...]}."""
    # Build already-routed connections to exclude from ratsnest
    routed: set[frozenset] = set()
    for route in board.routes:
        for seg in route.segments:
            routed.add(frozenset([seg.start, seg.end]))

    # Group absolute pad positions by net
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

        # Build all edges sorted by distance
        edges: list[tuple[float, int, int]] = []
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                dx = positions[i][0] - positions[j][0]
                dy = positions[i][1] - positions[j][1]
                edges.append((math.sqrt(dx * dx + dy * dy), i, j))
        edges.sort()

        # Kruskal's MST
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
# SVG helpers
# ---------------------------------------------------------------------------

def _pt(x: float, y: float, scale: float, mx: float, my: float) -> str:
    """Format a board-coordinate point as SVG px coordinates."""
    return f"{x * scale + mx:.2f},{y * scale + my:.2f}"


def _px(x: float, scale: float, mx: float) -> float:
    return x * scale + mx


def _py(y: float, scale: float, my: float) -> float:
    return y * scale + my


class SvgBuilder:
    def __init__(self) -> None:
        self._parts: list[str] = []

    def add(self, s: str) -> None:
        self._parts.append(s)

    def build(self) -> str:
        return "\n".join(self._parts)


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

def render_svg(board: Board, options: Optional[RenderOptions] = None) -> str:
    if options is None:
        options = RenderOptions()

    sc = options.scale
    mx = options.margin_px
    my = options.margin_px

    # Canvas size from board outline bounding box
    all_x = [pt[0] for pt in board.board_outline]
    all_y = [pt[1] for pt in board.board_outline]
    bw = (max(all_x) - min(all_x)) * sc + 2 * mx
    bh = (max(all_y) - min(all_y)) * sc + 2 * my

    # Build net index → color mapping
    net_colors: dict[str, str] = {}
    for idx, (name, net) in enumerate(board.nets.items()):
        net_colors[name] = _net_color(name, net.class_, idx)

    # Ratsnest
    ratsnest = _compute_ratsnest(board) if options.show_ratsnest else {}

    svg = SvgBuilder()
    svg.add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{bw:.0f}" height="{bh:.0f}" '
            f'style="background:#1a1a1a; font-family: monospace;">')
    svg.add("<defs>")
    svg.add("</defs>")

    # Board outline
    if options.show_outline and board.board_outline:
        pts = " ".join(_pt(x, y, sc, mx, my) for x, y in board.board_outline)
        svg.add(f'<polygon points="{pts}" fill="#222" stroke="#888" stroke-width="1"/>')

    # Keepouts
    if options.show_keepouts:
        for ko in board.keepouts:
            if ko.type == "circle" and ko.center and ko.radius:
                cx = _px(ko.center[0], sc, mx)
                cy = _py(ko.center[1], sc, my)
                r = ko.radius * sc
                svg.add(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
                        f'fill="none" stroke="#ff8800" stroke-width="1" stroke-dasharray="4,2"/>')
            elif ko.type == "rect" and ko.rect:
                rx, ry, rw, rh = ko.rect
                svg.add(f'<rect x="{_px(rx, sc, mx):.2f}" y="{_py(ry, sc, my):.2f}" '
                        f'width="{rw * sc:.2f}" height="{rh * sc:.2f}" '
                        f'fill="none" stroke="#ff8800" stroke-width="1" stroke-dasharray="4,2"/>')

    # Ratsnest
    if options.show_ratsnest:
        svg.add('<g id="ratsnest" opacity="0.5">')
        for net_name, edges in ratsnest.items():
            color = net_colors.get(net_name, "#888")
            for p1, p2 in edges:
                x1, y1 = _px(p1[0], sc, mx), _py(p1[1], sc, my)
                x2, y2 = _px(p2[0], sc, mx), _py(p2[1], sc, my)
                svg.add(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                        f'stroke="{color}" stroke-width="0.5" stroke-dasharray="3,2"/>')
        svg.add("</g>")

    # Routed traces
    if options.show_routes:
        svg.add('<g id="routes">')
        for route in board.routes:
            color = net_colors.get(route.net, "#888")
            w = max(route.width_mm * sc, 1.0)
            lc = _layer_color(route.segments[0].layer if route.segments else "F.Cu")
            for seg in route.segments:
                x1, y1 = _px(seg.start[0], sc, mx), _py(seg.start[1], sc, my)
                x2, y2 = _px(seg.end[0], sc, mx), _py(seg.end[1], sc, my)
                svg.add(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                        f'stroke="{lc}" stroke-width="{w:.1f}" stroke-linecap="round"/>')
        svg.add("</g>")

    # Vias
    if options.show_vias:
        for via in board.vias:
            vx = _px(via.position[0], sc, mx)
            vy = _py(via.position[1], sc, my)
            r = max(via.drill_mm * sc * 0.6, 2.0)
            color = net_colors.get(via.net, "#888")
            svg.add(f'<circle cx="{vx:.2f}" cy="{vy:.2f}" r="{r:.2f}" '
                    f'fill="{color}" stroke="#fff" stroke-width="0.5"/>')

    # Component bboxes + pads + labels
    if options.show_components:
        svg.add('<g id="components">')
        for ref, comp in board.components.items():
            cx, cy = comp.position
            x_min, y_min, x_max, y_max = comp.bbox
            # Rotate bbox corners and draw as polygon
            corners = [
                (x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max)
            ]
            rad = math.radians(-comp.rotation)  # KiCad uses clockwise-positive
            cos_r, sin_r = math.cos(rad), math.sin(rad)
            rotated = [
                (cx + dx * cos_r - dy * sin_r, cy + dx * sin_r + dy * cos_r)
                for dx, dy in corners
            ]
            pts = " ".join(_pt(x, y, sc, mx, my) for x, y in rotated)
            svg.add(f'<polygon points="{pts}" fill="none" stroke="#555" stroke-width="0.8"/>')

            # Reference label
            if options.show_labels:
                lx = _px(cx, sc, mx)
                ly = _py(cy, sc, my)
                svg.add(f'<text x="{lx:.1f}" y="{ly:.1f}" '
                        f'text-anchor="middle" dominant-baseline="middle" '
                        f'font-size="8" fill="#aaa">{ref}</text>')

        svg.add("</g>")

    # Pads
    if options.show_pads:
        svg.add('<g id="pads">')
        for comp in board.components.values():
            for pad in comp.pads:
                color = net_colors.get(pad.net, "#666") if options.pad_color_by_net and pad.net else "#666"
                ax, ay = comp.pad_abs_position(pad)
                pw = pad.size[0] * sc
                ph = pad.size[1] * sc
                px_c = _px(ax, sc, mx)
                py_c = _py(ay, sc, my)

                # Apply component rotation to the pad rectangle
                if comp.rotation != 0.0:
                    # Render as rotated rect using transform
                    svg.add(
                        f'<rect x="{px_c - pw / 2:.2f}" y="{py_c - ph / 2:.2f}" '
                        f'width="{pw:.2f}" height="{ph:.2f}" '
                        f'fill="{color}" stroke="#fff" stroke-width="0.3" '
                        f'transform="rotate({comp.rotation:.1f},{px_c:.2f},{py_c:.2f})"/>'
                    )
                elif pad.shape == "circle":
                    r = min(pw, ph) / 2
                    svg.add(f'<circle cx="{px_c:.2f}" cy="{py_c:.2f}" r="{r:.2f}" '
                            f'fill="{color}" stroke="#fff" stroke-width="0.3"/>')
                else:
                    svg.add(
                        f'<rect x="{px_c - pw / 2:.2f}" y="{py_c - ph / 2:.2f}" '
                        f'width="{pw:.2f}" height="{ph:.2f}" '
                        f'fill="{color}" stroke="#fff" stroke-width="0.3"/>'
                    )
        svg.add("</g>")

    svg.add("</svg>")
    return svg.build()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Render board.json as SVG")
    parser.add_argument("input", help="Input board.json file")
    parser.add_argument("-o", "--output", default="board.svg", help="Output SVG path")
    parser.add_argument("--scale", type=float, default=10.0, help="px per mm (default 10)")
    parser.add_argument("--show-ratsnest", action="store_true")
    parser.add_argument("--no-ratsnest", dest="show_ratsnest", action="store_false")
    parser.add_argument("--show-routes", action="store_true", default=True)
    parser.add_argument("--no-routes", dest="show_routes", action="store_false")
    parser.set_defaults(show_ratsnest=True)
    args = parser.parse_args()

    board = load_board(args.input)
    options = RenderOptions(
        scale=args.scale,
        show_ratsnest=args.show_ratsnest,
        show_routes=args.show_routes,
    )
    svg_content = render_svg(board, options)
    out_path = Path(args.output)
    out_path.write_text(svg_content)
    print(f"Rendered → {out_path}  ({len(svg_content):,} bytes)")


if __name__ == "__main__":
    main()
