"""Tests for common.vision.palette and common.vision.render.

Two things are load-bearing and easy to break silently:

  * the slug, because it names files and SVG ids and is what a caller passes
    back as ``?criterion=`` — a collision or an empty slug breaks the lookup;
  * the SVG's one-group-per-criterion structure, because a filtered render is
    only trustworthy if it really contains that criterion and nothing else.

So the SVG is parsed back with ElementTree rather than string-matched.
"""

from __future__ import annotations

import io
from xml.etree import ElementTree as ET

import pytest

from common.vision import (
    PageGeometry,
    Region,
    box_region,
    criterion_color,
    legend_entries,
    render_png_layer,
    render_preview,
    render_svg,
    slugify_criterion,
    stroke_for,
)

SVG_NS = "{http://www.w3.org/2000/svg}"


def _geom(page: int = 0, width: int = 400, height: int = 300) -> PageGeometry:
    return PageGeometry(page=page, width=width, height=height, working_scale=1.0)


def _regions() -> list[Region]:
    return [
        box_region((10, 10, 120, 90), "has faces", source="cv", score=0.8),
        box_region((150, 40, 260, 140), "has faces", source="cv", score=0.6),
        Region(page=0, kind="polygon", points=[(20, 200), (300, 200), (300, 280), (20, 280)],
               label="Notice to Owner", source="ocr", score=0.93,
               attrs={"text": "NOTICE TO OWNER"}),
        box_region((5, 5, 50, 50), "off page", page=1, source="cv"),
    ]


# ── Slugs ──────────────────────────────────────────────────────────────────
def test_slug_is_lowercase_and_hyphenated_with_a_hash():
    slug = slugify_criterion("Has Faces")
    assert slug.startswith("has-faces-")
    assert len(slug.split("-")[-1]) == 4


def test_slugs_that_would_collide_stay_distinct():
    a = slugify_criterion("has faces")
    b = slugify_criterion("Has Faces!")
    c = slugify_criterion("HAS---FACES")
    assert a.rsplit("-", 1)[0] == b.rsplit("-", 1)[0] == c.rsplit("-", 1)[0] == "has-faces"
    assert len({a, b, c}) == 3


def test_slug_is_stable_across_calls():
    assert slugify_criterion("roof condition") == slugify_criterion("roof condition")


def test_slug_folds_accents_and_survives_non_latin_names():
    assert slugify_criterion("Café Frontage").startswith("cafe-frontage-")
    # Nothing ASCII survives, so the base falls back to "c" — but it is still
    # unique per name.
    japanese = slugify_criterion("日本語")
    stars = slugify_criterion("***")
    assert japanese.startswith("c-") and stars.startswith("c-")
    assert japanese != stars


def test_slug_length_is_capped():
    slug = slugify_criterion("x" * 300)
    assert len(slug) <= 48 + 5


def test_colour_is_stable_and_differs_between_criteria():
    assert criterion_color("has faces") == criterion_color("has faces")
    assert criterion_color("has faces") != criterion_color("has sky")
    assert criterion_color("has faces").startswith("#")


def test_stroke_policy_matches_the_documented_table():
    assert stroke_for("cv")["dash"] is None
    assert stroke_for("pdf-text")["dash"] is None
    assert stroke_for("ocr")["dash"] is not None
    assert stroke_for("llm")["dash"] is not None
    assert stroke_for("diff")["double"] is True
    # An unknown source renders as plain solid rather than raising.
    assert stroke_for("something-new")["dash"] is None


def test_legend_has_one_row_per_criterion_and_source():
    entries = legend_entries([r for r in _regions() if r.page == 0])
    assert [(e["label"], e["source"], e["count"]) for e in entries] == [
        ("Notice to Owner", "ocr", 1),
        ("has faces", "cv", 2),
    ]


# ── SVG ────────────────────────────────────────────────────────────────────
def test_svg_is_well_formed_with_the_page_viewbox():
    svg = render_svg(_geom(), _regions())
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG_NS}svg"
    assert root.get("viewBox") == "0 0 400 300"


def test_svg_has_one_group_per_criterion_on_this_page():
    root = ET.fromstring(render_svg(_geom(), _regions()))
    groups = [g for g in root.findall(f"{SVG_NS}g") if g.get("id", "").startswith("c-")]
    assert len(groups) == 2
    ids = {g.get("id") for g in groups}
    assert f"c-{slugify_criterion('has faces')}" in ids
    assert f"c-{slugify_criterion('Notice to Owner')}" in ids
    # The page-1 region is not drawn onto page 0's overlay.
    assert "off page" not in render_svg(_geom(), _regions())


def test_svg_group_carries_criterion_name_source_and_shapes():
    root = ET.fromstring(render_svg(_geom(), _regions()))
    faces = root.find(f"{SVG_NS}g[@id='c-{slugify_criterion('has faces')}']")
    assert faces.get("data-criterion") == "has faces"
    assert faces.get("data-source") == "cv"
    rects = faces.findall(f"{SVG_NS}rect")
    assert len(rects) == 2
    assert rects[0].get("x") == "10.0" and rects[0].get("width") == "110.0"
    assert rects[0].find(f"{SVG_NS}title") is not None

    ocr = root.find(f"{SVG_NS}g[@id='c-{slugify_criterion('Notice to Owner')}']")
    polygons = ocr.findall(f"{SVG_NS}polygon")
    assert len(polygons) == 1
    assert polygons[0].get("stroke-dasharray")  # ocr draws dashed


def test_filtered_svg_contains_exactly_the_requested_criterion():
    regions = [r for r in _regions() if r.label == "has faces" and r.page == 0]
    root = ET.fromstring(render_svg(_geom(), regions))
    groups = [g for g in root.findall(f"{SVG_NS}g") if g.get("id", "").startswith("c-")]
    assert [g.get("data-criterion") for g in groups] == ["has faces"]


def test_svg_escapes_criterion_names_that_would_break_the_xml():
    region = box_region((0, 0, 10, 10), '<b>R&D "roof" </b>', source="cv")
    root = ET.fromstring(render_svg(_geom(), [region]))  # parses ⇒ escaped
    group = [g for g in root.findall(f"{SVG_NS}g") if g.get("id", "").startswith("c-")][0]
    assert group.get("data-criterion") == '<b>R&D "roof" </b>'


def test_svg_legend_can_be_switched_off():
    with_legend = ET.fromstring(render_svg(_geom(), _regions(), legend=True))
    without = ET.fromstring(render_svg(_geom(), _regions(), legend=False))
    assert with_legend.find(f"{SVG_NS}g[@id='legend']") is not None
    assert without.find(f"{SVG_NS}g[@id='legend']") is None


def test_svg_with_no_regions_is_still_valid():
    root = ET.fromstring(render_svg(_geom(), []))
    assert root.get("data-region-count") == "0"


# ── Raster layers ──────────────────────────────────────────────────────────
def test_png_layer_is_page_sized_rgba_and_mostly_transparent():
    PIL = pytest.importorskip("PIL.Image")
    png = render_png_layer(_geom(), _regions())
    img = PIL.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.mode == "RGBA"
    assert img.size == (400, 300)

    alpha = img.getchannel("A")
    # A corner nowhere near a region is fully transparent …
    assert alpha.getpixel((399, 0)) == 0
    # … while the inside of a box is not.
    assert alpha.getpixel((60, 50)) > 0


def test_png_layer_with_no_regions_is_fully_transparent():
    PIL = pytest.importorskip("PIL.Image")
    img = PIL.open(io.BytesIO(render_png_layer(_geom(), [])))
    assert img.getchannel("A").getextrema() == (0, 0)


def test_preview_is_a_jpeg_the_size_of_the_page():
    PIL = pytest.importorskip("PIL.Image")
    base = PIL.new("RGB", (400, 300), "white")
    buf = io.BytesIO()
    base.save(buf, format="PNG")

    out = render_preview(buf.getvalue(), _geom(), _regions())
    img = PIL.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (400, 300)
    # Something was burned in — a blank white page would stay white.
    assert img.convert("RGB").getpixel((60, 50)) != (255, 255, 255)


def test_preview_resizes_a_base_image_that_does_not_match_the_geometry():
    PIL = pytest.importorskip("PIL.Image")
    small = PIL.new("RGB", (200, 150), "white")
    out = render_preview(small, _geom(), _regions())
    assert PIL.open(io.BytesIO(out)).size == (400, 300)
