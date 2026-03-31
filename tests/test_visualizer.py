"""Tests for src/visualizer.py."""
import math
import xml.etree.ElementTree as ET

import pytest

from src.schema import Component, Net, Pad, Placement, Route, Segment, Via
from src.visualizer import RenderOptions, _compute_ratsnest, _net_color, render_svg


# ---------------------------------------------------------------------------
# Net color
# ---------------------------------------------------------------------------

class TestNetColor:
    def test_ground_is_dark(self):
        color = _net_color("GND", "ground", 0)
        assert color == "#222222"

    def test_power_is_red(self):
        color = _net_color("3V3", "power", 1)
        assert color == "#cc2222"

    def test_signal_uses_hsl(self):
        color = _net_color("SPI_CLK", "signal", 0)
        assert color.startswith("hsl(")

    def test_signal_colors_are_deterministic(self):
        c1 = _net_color("SPI_CLK", "signal", 0)
        c2 = _net_color("SPI_CLK", "signal", 0)
        assert c1 == c2

    def test_different_indices_different_colors(self):
        c0 = _net_color("NET_A", "signal", 0)
        c1 = _net_color("NET_B", "signal", 1)
        assert c0 != c1


# ---------------------------------------------------------------------------
# Ratsnest MST
# ---------------------------------------------------------------------------

class TestComputeRatsnest:
    def test_two_pad_net_has_one_edge(self, minimal_board):
        ratsnest = _compute_ratsnest(minimal_board)
        gnd = ratsnest.get("GND", [])
        assert len(gnd) == 1

    def test_mst_edge_count(self, minimal_board):
        # N pads → N-1 edges in MST
        ratsnest = _compute_ratsnest(minimal_board)
        for net_name, edges in ratsnest.items():
            net_pads = [
                p for comp in minimal_board.components.values()
                for p in comp.pads if p.net == net_name
            ]
            assert len(edges) == len(net_pads) - 1

    def test_single_pad_net_excluded(self, minimal_board):
        # Add a net with only one pad
        from src.schema import Net
        minimal_board.nets["LONELY"] = Net(
            name="LONELY", class_="signal", strategy="route", width_mm=0.3, priority=5
        )
        minimal_board.components["U1"].pads.append(
            Pad(number="99", net="LONELY", offset=(0.0, 5.0), size=(0.6, 0.6), shape="rect", layer="F.Cu")
        )
        ratsnest = _compute_ratsnest(minimal_board)
        assert "LONELY" not in ratsnest

    def test_triangle_has_two_mst_edges(self, minimal_board):
        """3 pads → MST has exactly 2 edges."""
        # Add a third pad to GND on U1
        minimal_board.components["U1"].pads.append(
            Pad(number="3", net="GND", offset=(0.0, 3.0), size=(0.6, 0.6), shape="rect", layer="F.Cu")
        )
        ratsnest = _compute_ratsnest(minimal_board)
        gnd_edges = ratsnest.get("GND", [])
        assert len(gnd_edges) == 2


# ---------------------------------------------------------------------------
# SVG output validity and content
# ---------------------------------------------------------------------------

class TestRenderSvg:
    def test_produces_valid_xml(self, minimal_board):
        svg = render_svg(minimal_board)
        # Should not raise
        root = ET.fromstring(svg)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"

    def test_svg_has_dimensions(self, minimal_board):
        svg = render_svg(minimal_board)
        root = ET.fromstring(svg)
        assert "width" in root.attrib
        assert "height" in root.attrib
        assert float(root.attrib["width"]) > 0
        assert float(root.attrib["height"]) > 0

    def test_svg_width_scales_with_board(self, minimal_board):
        opt10 = RenderOptions(scale=10.0, margin_px=20.0)
        opt20 = RenderOptions(scale=20.0, margin_px=20.0)
        svg10 = render_svg(minimal_board, opt10)
        svg20 = render_svg(minimal_board, opt20)
        root10 = ET.fromstring(svg10)
        root20 = ET.fromstring(svg20)
        w10 = float(root10.attrib["width"])
        w20 = float(root20.attrib["width"])
        # Board is wider than 2*margin, so doubling scale roughly doubles width
        assert w20 > w10

    def test_contains_component_references(self, minimal_board):
        svg = render_svg(minimal_board)
        assert "U1" in svg
        assert "J1" in svg

    def test_no_ratsnest_option(self, minimal_board):
        opts = RenderOptions(show_ratsnest=False)
        svg = render_svg(minimal_board, opts)
        # Still produces valid SVG
        ET.fromstring(svg)
        assert 'id="ratsnest"' not in svg

    def test_with_ratsnest(self, minimal_board):
        opts = RenderOptions(show_ratsnest=True)
        svg = render_svg(minimal_board, opts)
        assert 'id="ratsnest"' in svg

    def test_routes_rendered(self, minimal_board):
        minimal_board.routes.append(
            Route(net="3V3", width_mm=0.5, segments=[
                Segment(start=(9.0, 9.0), end=(30.0, 9.0), layer="F.Cu")
            ])
        )
        svg = render_svg(minimal_board, RenderOptions(show_routes=True))
        assert 'id="routes"' in svg
        assert "<line" in svg

    def test_vias_rendered(self, minimal_board):
        minimal_board.vias.append(Via(position=(15.0, 9.0), net="GND", drill_mm=0.3))
        svg = render_svg(minimal_board)
        # Via is a circle element (beyond just the board outline circle-keepouts)
        assert "<circle" in svg

    def test_full_export_pipeline(self, synthetic_kicad_path, tmp_path):
        """End-to-end: export .kicad_pcb → board.json → SVG."""
        from src.kicad_export import export_board
        from src.schema import save_board, load_board

        board = export_board(synthetic_kicad_path)
        json_path = tmp_path / "board.json"
        save_board(board, json_path)

        board2 = load_board(json_path)
        svg = render_svg(board2, RenderOptions(show_ratsnest=True))

        root = ET.fromstring(svg)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert "U1" in svg
        assert "J1" in svg
