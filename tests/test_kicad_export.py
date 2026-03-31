"""Tests for src/kicad_export.py."""
import pytest

from src.kicad_export import (
    _classify_net, _compute_bbox, _connect_segments, _extract_outline,
    export_board,
)
from src.schema import Pad
from src.sexpr_parser import parse


# ---------------------------------------------------------------------------
# Net classification
# ---------------------------------------------------------------------------

class TestClassifyNet:
    def test_gnd(self):
        class_, strategy, width, priority = _classify_net("GND")
        assert class_ == "ground"
        assert strategy == "pour"
        assert width is None
        assert priority == 0

    def test_agnd(self):
        assert _classify_net("AGND")[0] == "ground"

    def test_3v3(self):
        class_, strategy, width, priority = _classify_net("3V3")
        assert class_ == "power"
        assert strategy == "route_wide"
        assert width == 0.5
        assert priority == 1

    def test_vcc(self):
        assert _classify_net("VCC")[0] == "power"

    def test_vbus(self):
        assert _classify_net("VBUS")[0] == "power"

    def test_spi_clk(self):
        class_, _, _, priority = _classify_net("SPI_CLK")
        assert class_ == "constrained_signal"
        assert priority == 2

    def test_uart_tx(self):
        class_, _, _, _ = _classify_net("UART_TX")
        assert class_ == "signal"

    def test_i2c_sda(self):
        class_, _, _, _ = _classify_net("I2C_SDA")
        assert class_ == "signal"

    def test_usb_dp(self):
        class_, _, _, _ = _classify_net("USB_DP")
        assert class_ == "constrained_signal"


# ---------------------------------------------------------------------------
# Outline extraction
# ---------------------------------------------------------------------------

class TestConnectSegments:
    def test_rectangle(self):
        segs = [
            ((0.0, 0.0), (60.0, 0.0)),
            ((60.0, 0.0), (60.0, 40.0)),
            ((60.0, 40.0), (0.0, 40.0)),
            ((0.0, 40.0), (0.0, 0.0)),
        ]
        polygon = _connect_segments(segs)
        assert len(polygon) == 4
        assert (0.0, 0.0) in polygon
        assert (60.0, 0.0) in polygon

    def test_closed_loop_deduplication(self):
        # Segments that form a triangle, last point == first
        segs = [((0.0, 0.0), (1.0, 0.0)), ((1.0, 0.0), (0.5, 1.0)), ((0.5, 1.0), (0.0, 0.0))]
        polygon = _connect_segments(segs)
        assert len(polygon) == 3

    def test_single_segment(self):
        segs = [((0.0, 0.0), (10.0, 0.0))]
        polygon = _connect_segments(segs)
        assert len(polygon) == 2


class TestExtractOutline:
    def test_extracts_from_fixture(self, synthetic_kicad_path):
        from src.sexpr_parser import parse_file
        tree = parse_file(synthetic_kicad_path)
        outline = _extract_outline(tree)
        assert len(outline) == 4

    def test_bounding_box_is_correct(self, synthetic_kicad_path):
        from src.sexpr_parser import parse_file
        tree = parse_file(synthetic_kicad_path)
        outline = _extract_outline(tree)
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        # Fixture board: 100–160 in X (60mm), 80–120 in Y (40mm)
        assert min(xs) == 100.0
        assert max(xs) == 160.0
        assert min(ys) == 80.0
        assert max(ys) == 120.0


# ---------------------------------------------------------------------------
# BBox computation
# ---------------------------------------------------------------------------

class TestComputeBbox:
    def test_symmetric_pads(self):
        pads = [
            Pad(number="1", net="GND", offset=(-3.5, -4.0), size=(0.6, 1.2), shape="rect", layer="F.Cu"),
            Pad(number="2", net="VCC", offset=(3.5, 4.0), size=(0.6, 1.2), shape="rect", layer="F.Cu"),
        ]
        x_min, y_min, x_max, y_max = _compute_bbox(pads)
        assert x_min < -3.5
        assert x_max > 3.5
        assert y_min < -4.0
        assert y_max > 4.0

    def test_empty_pads(self):
        bbox = _compute_bbox([])
        assert len(bbox) == 4


# ---------------------------------------------------------------------------
# Full export
# ---------------------------------------------------------------------------

class TestExportBoard:
    def test_exports_synthetic_board(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        assert board is not None

    def test_correct_component_count(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        assert len(board.components) == 5
        assert "U1" in board.components
        assert "J1" in board.components
        assert "J2" in board.components
        assert "C1" in board.components
        assert "C2" in board.components

    def test_correct_net_count(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        # 8 named nets (not counting net 0 "")
        assert len(board.nets) == 8
        assert "GND" in board.nets
        assert "3V3" in board.nets
        assert "SPI_CLK" in board.nets

    def test_net_classification(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        assert board.nets["GND"].class_ == "ground"
        assert board.nets["3V3"].class_ == "power"
        assert board.nets["SPI_CLK"].class_ == "constrained_signal"
        assert board.nets["I2C_SDA"].class_ == "signal"

    def test_board_outline_is_translated(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        # After origin subtraction, board should start at (0,0)
        xs = [pt[0] for pt in board.board_outline]
        ys = [pt[1] for pt in board.board_outline]
        assert min(xs) == pytest.approx(0.0, abs=0.01)
        assert min(ys) == pytest.approx(0.0, abs=0.01)

    def test_board_dimensions(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        xs = [pt[0] for pt in board.board_outline]
        ys = [pt[1] for pt in board.board_outline]
        # Fixture: 60×40mm board. 40mm snaps to 39.9 at 0.3mm grid (40/0.3 = 133.33 → 39.9).
        assert max(xs) == pytest.approx(60.0, abs=0.01)
        assert max(ys) == pytest.approx(40.0, abs=0.15)  # within half a grid step

    def test_u1_position(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        u1 = board.components["U1"]
        # U1 at page (130, 101), origin (100, 80) → board (30, 21)
        assert u1.position[0] == pytest.approx(30.0, abs=0.01)
        assert u1.position[1] == pytest.approx(21.0, abs=0.01)

    def test_u1_has_10_pads(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        assert len(board.components["U1"].pads) == 10

    def test_pad_net_assignment(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        u1 = board.components["U1"]
        pad1 = next(p for p in u1.pads if p.number == "1")
        assert pad1.net == "GND"
        pad3 = next(p for p in u1.pads if p.number == "3")
        assert pad3.net == "SPI_CLK"

    def test_thru_hole_pad_layer(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        j2 = board.components["J2"]
        pad1 = j2.pads[0]
        assert pad1.layer == "*.Cu"

    def test_ground_pour_added(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path)
        assert len(board.pours) >= 1
        assert board.pours[0].net == "GND"
        assert board.pours[0].layer == "B.Cu"

    def test_grid_step_preserved(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path, grid=0.3)
        assert board.grid_step == 0.3

    def test_custom_grid(self, synthetic_kicad_path):
        board = export_board(synthetic_kicad_path, grid=0.1)
        assert board.grid_step == 0.1

    def test_all_coords_on_grid(self, synthetic_kicad_path):
        from src.schema import snap_to_grid
        board = export_board(synthetic_kicad_path, grid=0.3)
        for comp in board.components.values():
            x, y = comp.position
            assert x == pytest.approx(snap_to_grid(x, 0.3), abs=1e-6), \
                f"{comp.reference} x={x} not on grid"
            assert y == pytest.approx(snap_to_grid(y, 0.3), abs=1e-6), \
                f"{comp.reference} y={y} not on grid"
