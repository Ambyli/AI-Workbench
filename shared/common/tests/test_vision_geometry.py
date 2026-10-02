"""Tests for common.vision.model + common.vision.geometry.

The coordinate contract ("regions are always in original page pixels") is the
one thing every other part of the feature trusts, so these tests are mostly
round-trips: a value that goes working → original → working, or pixels →
points → pixels, has to come back where it started.
"""

from __future__ import annotations

import math

import pytest

from common.vision import (
    PageGeometry,
    Region,
    REGION_SOURCES,
    box_region,
    center_distance,
    clamp_points,
    grid_to_pixels,
    iou,
    normalize_region,
    pixels_to_grid,
    pixels_to_points,
    points_to_pixels,
    rescale_region,
    to_original,
    to_working,
)


def _geom(**kwargs) -> PageGeometry:
    base = {"page": 0, "width": 3024, "height": 4032, "working_scale": 1000 / 4032}
    base.update(kwargs)
    return PageGeometry(**base)


# ── working ↔ original ─────────────────────────────────────────────────────
def test_working_to_original_round_trip():
    geom = _geom()
    points = [(10.0, 20.0), (740.0, 991.5)]
    there = to_original(points, geom.working_scale)
    back = to_working(there, geom.working_scale)
    for (x0, y0), (x1, y1) in zip(points, back):
        assert math.isclose(x0, x1, rel_tol=1e-9)
        assert math.isclose(y0, y1, rel_tol=1e-9)


def test_to_original_scales_up_by_the_inverse():
    # A box filling the 1000-px working image must fill the original page.
    geom = _geom()
    points = to_original([(0.0, 0.0), (750.0, 1000.0)], geom.working_scale)
    assert math.isclose(points[1][0], 3024.0, rel_tol=1e-6)
    assert math.isclose(points[1][1], 4032.0, rel_tol=1e-6)


def test_zero_working_scale_is_treated_as_identity():
    # A geometry bookkeeping slip must not destroy a detector's output.
    assert to_original([(5.0, 7.0)], 0.0) == [(5.0, 7.0)]
    assert to_working([(5.0, 7.0)], 0.0) == [(5.0, 7.0)]


def test_rescale_region_clamps_and_stamps_the_page():
    geom = _geom(page=3)
    region = Region(page=0, kind="box", points=[(-5.0, -5.0), (1200.0, 1200.0)],
                    label="has faces", source="cv")
    out = rescale_region(region, geom.working_scale, geom)
    assert out.page == 3
    assert out.points[0] == (0.0, 0.0)
    assert out.points[1] == (float(geom.width), float(geom.height))
    # The input is untouched.
    assert region.points[0] == (-5.0, -5.0)


# ── pixels ↔ PDF points ────────────────────────────────────────────────────
def test_pixels_to_points_round_trip():
    geom = PageGeometry(page=0, width=1275, height=1650, working_scale=0.606,
                        pdf_points=(612.0, 792.0))
    points = [(100.0, 200.0), (900.0, 1400.0)]
    there = pixels_to_points(points, geom)
    back = points_to_pixels(there, geom)
    for (x0, y0), (x1, y1) in zip(points, back):
        assert math.isclose(x0, x1, rel_tol=1e-9)
        assert math.isclose(y0, y1, rel_tol=1e-9)


def test_pixels_to_points_matches_the_dpi_ratio():
    # 1275 px across a 612 pt page is 150 dpi; half the page in px is half in pt.
    geom = PageGeometry(page=0, width=1275, height=1650, pdf_points=(612.0, 792.0))
    (x, y), = pixels_to_points([(1275 / 2, 1650 / 2)], geom)
    assert math.isclose(x, 306.0, rel_tol=1e-9)
    assert math.isclose(y, 396.0, rel_tol=1e-9)


def test_point_conversions_are_none_without_pdf_points():
    geom = _geom()
    assert pixels_to_points([(1.0, 1.0)], geom) is None
    assert points_to_pixels([(1.0, 1.0)], geom) is None


# ── 0–1000 grid (the space a vision model answers in) ──────────────────────
def test_grid_round_trip():
    geom = _geom()
    bbox = [120.0, 340.0, 700.0, 880.0]
    pixels = grid_to_pixels(bbox, geom)
    back = pixels_to_grid(pixels, geom)
    for a, b in zip(bbox, back):
        assert math.isclose(a, b, rel_tol=1e-9)


def test_full_grid_box_covers_the_whole_page():
    geom = _geom()
    (x1, y1), (x2, y2) = grid_to_pixels([0, 0, 1000, 1000], geom)
    assert (x1, y1) == (0.0, 0.0)
    assert math.isclose(x2, geom.width) and math.isclose(y2, geom.height)


# ── Region helpers ─────────────────────────────────────────────────────────
def test_box_region_normalises_corners():
    region = box_region((200, 300, 100, 50), "has sky", page=2, source="cv")
    assert region.points == [(100.0, 50.0), (200.0, 300.0)]
    assert region.page == 2
    assert region.kind == "box"


def test_region_area_box_and_polygon_agree_on_a_rectangle():
    box = box_region((0, 0, 10, 20), "x")
    poly = Region(page=0, kind="polygon",
                  points=[(0, 0), (10, 0), (10, 20), (0, 20)], label="x")
    assert math.isclose(box.area(), 200.0)
    assert math.isclose(poly.area(), 200.0)


def test_region_dict_round_trip_preserves_everything():
    region = Region(page=1, kind="polygon", points=[(1.234, 2.345), (3.0, 4.0), (5.0, 6.0)],
                    label="Notice to Owner", score=0.91, source="ocr",
                    attrs={"text": "NOTICE TO OWNER", "line": 2})
    rebuilt = Region.from_dict(region.as_dict())
    assert rebuilt.page == 1
    assert rebuilt.label == "Notice to Owner"
    assert rebuilt.source == "ocr"
    assert rebuilt.attrs["line"] == 2
    assert rebuilt.points[0] == (1.23, 2.35)  # as_dict rounds to 2 dp on purpose


def test_page_geometry_dict_round_trip():
    geom = PageGeometry(page=4, width=100, height=200, working_scale=0.5,
                        pdf_points=(612.0, 792.0))
    rebuilt = PageGeometry.from_dict(geom.as_dict())
    assert rebuilt == geom


def test_clamp_points_pulls_coordinates_inside_the_page():
    assert clamp_points([(-3.0, 5.0), (120.0, 500.0)], 100, 200) == [
        (0.0, 5.0),
        (100.0, 200.0),
    ]


def test_iou_identical_boxes_is_one_and_disjoint_is_zero():
    a = box_region((0, 0, 10, 10), "a")
    b = box_region((0, 0, 10, 10), "b")
    c = box_region((50, 50, 60, 60), "c")
    assert math.isclose(iou(a, b), 1.0)
    assert iou(a, c) == 0.0


def test_iou_half_overlap():
    a = box_region((0, 0, 10, 10), "a")
    b = box_region((5, 0, 15, 10), "b")
    # intersection 50, union 150
    assert math.isclose(iou(a, b), 1 / 3, rel_tol=1e-9)


# ── comparing regions across pages ─────────────────────────────────────────
def test_normalize_region_is_a_fraction_of_its_own_page():
    geom = _geom(width=2000, height=1000)
    region = box_region((200, 100, 1000, 500), "a", source="manual", attrs={"k": 1})
    unit = normalize_region(region, geom)
    assert unit.points == [(0.1, 0.1), (0.5, 0.5)]
    assert (unit.label, unit.source, unit.attrs) == ("a", "manual", {"k": 1})
    # The input is untouched.
    assert region.points == [(200.0, 100.0), (1000.0, 500.0)]


def test_the_same_layout_on_two_page_sizes_normalises_identically():
    portrait = _geom(width=1000, height=2000)
    landscape = _geom(width=3000, height=1500)
    a = normalize_region(box_region((100, 200, 300, 600), "a"), portrait)
    b = normalize_region(box_region((300, 150, 900, 450), "b"), landscape)
    assert math.isclose(iou(a, b), 1.0)
    assert center_distance(a, b) == pytest.approx(0.0)


def test_normalize_region_clamps_and_survives_a_zero_page():
    unit = normalize_region(box_region((-10, 0, 1100, 50), "a"), _geom(width=1000, height=100))
    assert unit.points == [(0.0, 0.0), (1.0, 0.5)]
    flat = normalize_region(box_region((0, 0, 10, 10), "a"), _geom(width=0, height=0))
    assert flat.points == [(0.0, 0.0), (0.0, 0.0)]


def test_center_distance_spans_zero_to_root_two_on_the_unit_square():
    top_left = box_region((0, 0, 0, 0), "a")
    bottom_right = box_region((1, 1, 1, 1), "b")
    assert center_distance(top_left, top_left) == 0.0
    assert math.isclose(center_distance(top_left, bottom_right), math.sqrt(2))
    # Centres (0.25, 0.25) and (0.75, 0.25): half the width apart.
    left = box_region((0.0, 0.0, 0.5, 0.5), "l")
    right = box_region((0.5, 0.0, 1.0, 0.5), "r")
    assert math.isclose(center_distance(left, right), 0.5)


def test_manual_is_a_region_source():
    assert "manual" in REGION_SOURCES
