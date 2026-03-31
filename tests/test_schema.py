"""Tests for src/schema.py."""
import json
import math
import tempfile
from pathlib import Path

import pytest

from src.schema import (
    Board, Component, Keepout, Net, Pad, Placement, Pour, Route, Rules,
    Segment, Via, board_from_dict, board_to_dict, load_board, save_board,
    snap_to_grid, validate_board,
)


# ---------------------------------------------------------------------------
# snap_to_grid
# ---------------------------------------------------------------------------

class TestSnapToGrid:
    def test_exact_grid_value(self):
        assert snap_to_grid(0.3) == 0.3
        assert snap_to_grid(0.6) == 0.6
        assert snap_to_grid(30.0) == 30.0

    def test_rounds_to_nearest(self):
        assert snap_to_grid(0.31) == 0.3
        assert snap_to_grid(0.29) == 0.3
        assert snap_to_grid(0.44) == 0.3
        assert snap_to_grid(0.46) == 0.6

    def test_zero(self):
        assert snap_to_grid(0.0) == 0.0

    def test_negative(self):
        assert snap_to_grid(-0.3) == -0.3
        assert snap_to_grid(-0.31) == -0.3

    def test_custom_grid(self):
        assert snap_to_grid(1.0, grid=0.25) == 1.0
        assert snap_to_grid(1.1, grid=0.25) == 1.0
        assert snap_to_grid(1.13, grid=0.25) == 1.25

    def test_output_precision(self):
        result = snap_to_grid(10.0)
        assert isinstance(result, float)
        # Should have at most 4 decimal places
        assert result == round(result, 4)


# ---------------------------------------------------------------------------
# pad_abs_position
# ---------------------------------------------------------------------------

class TestPadAbsPosition:
    def _make_component(self, pos, rotation, pad_offset):
        pad = Pad(number="1", net="GND", offset=pad_offset, size=(0.6, 0.6), shape="rect", layer="F.Cu")
        return Component(
            reference="U1", footprint="FP", description="",
            position=pos, rotation=rotation, layer="F.Cu",
            bbox=(-2.0, -2.0, 2.0, 2.0), pads=[pad],
        )

    def test_no_rotation(self):
        comp = self._make_component((10.0, 20.0), 0.0, (3.0, 4.0))
        x, y = comp.pad_abs_position(comp.pads[0])
        assert abs(x - 13.0) < 1e-9
        assert abs(y - 24.0) < 1e-9

    def test_90_rotation(self):
        comp = self._make_component((10.0, 20.0), 90.0, (3.0, 0.0))
        x, y = comp.pad_abs_position(comp.pads[0])
        # KiCad 90° CW: (3, 0) → (0, -3)
        assert abs(x - 10.0) < 1e-9
        assert abs(y - 17.0) < 1e-9

    def test_180_rotation(self):
        comp = self._make_component((10.0, 20.0), 180.0, (3.0, 4.0))
        x, y = comp.pad_abs_position(comp.pads[0])
        # 180° is the same CW or CCW
        assert abs(x - 7.0) < 1e-9
        assert abs(y - 16.0) < 1e-9

    def test_270_rotation(self):
        comp = self._make_component((10.0, 20.0), 270.0, (3.0, 0.0))
        x, y = comp.pad_abs_position(comp.pads[0])
        # KiCad 270° CW = 90° CCW: (3, 0) → (0, 3)
        assert abs(x - 10.0) < 1e-9
        assert abs(y - 23.0) < 1e-9

    def test_at_origin(self):
        comp = self._make_component((0.0, 0.0), 0.0, (1.0, 2.0))
        x, y = comp.pad_abs_position(comp.pads[0])
        assert abs(x - 1.0) < 1e-9
        assert abs(y - 2.0) < 1e-9


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestBoardRoundTrip:
    def test_round_trip_preserves_structure(self, minimal_board):
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)

        assert restored.grid_step == minimal_board.grid_step
        assert len(restored.components) == len(minimal_board.components)
        assert set(restored.nets.keys()) == set(minimal_board.nets.keys())
        assert len(restored.pours) == len(minimal_board.pours)

    def test_round_trip_preserves_positions(self, minimal_board):
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)

        u1 = restored.components["U1"]
        assert u1.position == (9.0, 9.0)
        assert u1.rotation == 0.0

    def test_round_trip_preserves_pads(self, minimal_board):
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)

        pads = restored.components["U1"].pads
        assert len(pads) == 2
        assert pads[0].net == "GND"
        assert pads[1].net == "3V3"
        assert pads[0].offset == (-1.5, 0.0)

    def test_round_trip_net_class_key(self, minimal_board):
        d = board_to_dict(minimal_board)
        # JSON uses "class" not "class_"
        assert "class" in d["nets"]["GND"]
        assert d["nets"]["GND"]["class"] == "ground"

    def test_round_trip_preserves_net_class(self, minimal_board):
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)
        assert restored.nets["GND"].class_ == "ground"
        assert restored.nets["3V3"].class_ == "power"

    def test_board_outline_is_list_of_pairs(self, minimal_board):
        d = board_to_dict(minimal_board)
        outline = d["board_outline"]
        assert isinstance(outline, list)
        assert all(isinstance(pt, list) and len(pt) == 2 for pt in outline)

    def test_none_width_preserved(self, minimal_board):
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)
        assert restored.nets["GND"].width_mm is None

    def test_keepout_round_trip(self, minimal_board):
        from src.schema import Keepout
        minimal_board.keepouts.append(
            Keepout(type="circle", layers=["*.Cu"], center=(5.0, 5.0), radius=1.6, notes="Mount hole")
        )
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)
        k = restored.keepouts[0]
        assert k.type == "circle"
        assert k.center == (5.0, 5.0)
        assert k.radius == 1.6

    def test_route_round_trip(self, minimal_board):
        minimal_board.routes.append(
            Route(net="3V3", width_mm=0.5, segments=[
                Segment(start=(5.0, 5.0), end=(15.0, 5.0), layer="F.Cu"),
            ])
        )
        d = board_to_dict(minimal_board)
        restored = board_from_dict(d)
        r = restored.routes[0]
        assert r.net == "3V3"
        assert r.segments[0].start == (5.0, 5.0)
        assert r.segments[0].layer == "F.Cu"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

class TestFileIO:
    def test_save_and_load(self, minimal_board, tmp_path):
        path = tmp_path / "test_board.json"
        save_board(minimal_board, path)

        assert path.exists()
        restored = load_board(path)
        assert len(restored.components) == 2

    def test_saved_json_is_valid(self, minimal_board, tmp_path):
        path = tmp_path / "test_board.json"
        save_board(minimal_board, path)

        with open(path) as f:
            data = json.load(f)
        assert "board_outline" in data
        assert "components" in data
        assert "nets" in data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_board_has_no_errors(self, minimal_board):
        errors = validate_board(minimal_board)
        assert errors == []

    def test_short_outline_fails(self, minimal_board):
        minimal_board.board_outline = [(0.0, 0.0), (10.0, 0.0)]
        errors = validate_board(minimal_board)
        assert any("board_outline" in e for e in errors)

    def test_invalid_layer_fails(self, minimal_board):
        minimal_board.components["U1"].layer = "G.Cu"
        errors = validate_board(minimal_board)
        assert any("U1" in e and "layer" in e for e in errors)

    def test_unknown_net_in_pad_fails(self, minimal_board):
        minimal_board.components["U1"].pads[0].net = "NONEXISTENT"
        errors = validate_board(minimal_board)
        assert any("NONEXISTENT" in e for e in errors)

    def test_unknown_net_class_fails(self, minimal_board):
        minimal_board.nets["GND"].class_ = "unknown_class"
        errors = validate_board(minimal_board)
        assert any("ground" not in e or "unknown_class" in e for e in errors)

    def test_route_unknown_net_fails(self, minimal_board):
        minimal_board.routes.append(
            Route(net="GHOST", width_mm=0.3, segments=[])
        )
        errors = validate_board(minimal_board)
        assert any("GHOST" in e for e in errors)
