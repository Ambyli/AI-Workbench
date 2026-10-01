"""Tests for common.vision.annotate.

The module exists to be a SECOND opinion on geometry the service already
rendered, so the things worth pinning are the ones that would make it agree
with the service for the wrong reason, or disagree for no reason:

  * the caller's image is never modified, and the output frame is the
    geometry's (a review picture at the wrong size is a silently wrong
    overlay);
  * regions belonging to another page are not drawn;
  * a mis-sized ``labels`` sequence raises instead of drawing captions
    against the wrong boxes — a mislabelled box reads as evidence;
  * ink actually lands on the region, and only there when the page is empty.
"""

from __future__ import annotations

import io

import pytest

from common.vision import (
    PageGeometry,
    Region,
    annotate_to_jpeg,
    box_region,
    default_label,
    draw_regions,
    geometry_for_image,
    is_rejected,
    regions_from_json,
    safe_text,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402  (after the importorskip guard)


def _page(width: int = 400, height: int = 300) -> "Image.Image":
    return Image.new("RGB", (width, height), "white")


def _geom(page: int = 0, width: int = 400, height: int = 300) -> PageGeometry:
    return PageGeometry(page=page, width=width, height=height, working_scale=1.0)


def _box() -> Region:
    return box_region((40, 40, 200, 160), "has faces", source="cv", score=0.81)


def _polygon() -> Region:
    return Region(
        page=0,
        kind="polygon",
        points=[(220, 60), (380, 70), (375, 200), (215, 190)],
        label="Notice to Owner",
        source="ocr",
        score=0.97,
        attrs={"text": "NOTICE TO OWNER"},
    )


def _rejected() -> Region:
    return box_region(
        (10, 210, 390, 290),
        "has solar panels",
        source="llm",
        attrs={"attempt": 1, "accepted": False, "reject": "too large"},
    )


# ── Contract ──────────────────────────────────────────────────────────────


def test_returns_new_rgb_image_and_leaves_the_input_alone():
    page = _page()
    before = page.tobytes()
    out = draw_regions(page, [_box()], geometry=_geom())
    assert out is not page
    assert out.mode == "RGB"
    assert out.size == (400, 300)
    assert page.tobytes() == before


def test_output_takes_the_geometry_frame_not_the_image_size():
    """A page rendered at a different DPI is resized to the geometry."""
    out = draw_regions(_page(200, 150), [_box()], geometry=_geom(width=400, height=300))
    assert out.size == (400, 300)


def test_geometry_defaults_to_the_image_itself():
    out = draw_regions(_page(321, 222), [])
    assert out.size == (321, 222)
    assert geometry_for_image(_page(321, 222)).width == 321


def test_regions_on_another_page_are_not_drawn():
    other = box_region((40, 40, 200, 160), "has faces", page=3, source="cv")
    out = draw_regions(_page(), [other], geometry=_geom(page=0))
    assert out.tobytes() == _page().tobytes()


def test_ink_lands_inside_the_region_and_the_page_is_otherwise_clean():
    out = draw_regions(_page(), [_box()], geometry=_geom(), label_regions=False)
    pixels = out.load()
    # A point on the outline is no longer white…
    assert pixels[40, 100] != (255, 255, 255)
    # …and a point far from it still is.
    assert pixels[350, 280] == (255, 255, 255)


def test_captions_are_drawn_when_asked_and_not_when_not():
    regions = [_box()]
    with_labels = draw_regions(_page(), regions, geometry=_geom(), label_regions=True)
    without = draw_regions(_page(), regions, geometry=_geom(), label_regions=False)
    assert with_labels.tobytes() != without.tobytes()


# ── Labels ────────────────────────────────────────────────────────────────


def test_default_label_names_the_criterion_score_and_source():
    assert default_label(_box()) == "has faces · 0.81 · cv"
    assert default_label(
        box_region((0, 0, 1, 1), "has sky", source="cv")
    ) == "has sky · cv"


def test_labels_accepts_a_callable():
    seen: list[str] = []

    def label(region: Region) -> str:
        seen.append(region.label)
        return f"{region.label} · PASS"

    draw_regions(_page(), [_box(), _polygon()], geometry=_geom(), labels=label)
    assert seen == ["has faces", "Notice to Owner"]


def test_labels_sequence_must_match_the_region_count():
    with pytest.raises(ValueError, match="one caption per region"):
        draw_regions(_page(), [_box(), _polygon()], geometry=_geom(), labels=["only one"])


def test_labels_sequence_of_the_right_length_is_accepted():
    out = draw_regions(
        _page(), [_box(), _polygon()], geometry=_geom(), labels=["a", "b"]
    )
    assert out.size == (400, 300)


# ── Rejected attempts ─────────────────────────────────────────────────────


def test_is_rejected_reads_the_enforcement_loops_flag():
    assert is_rejected(_rejected()) is True
    assert is_rejected(_box()) is False
    # An accepted attempt is not "rejected", and neither is a region with no
    # notion of acceptance at all.
    accepted = box_region((1, 1, 2, 2), "x", source="llm", attrs={"accepted": True})
    assert is_rejected(accepted) is False


def test_a_rejected_region_is_drawn_differently_from_an_accepted_one():
    rejected = _rejected()
    accepted = box_region(
        (10, 210, 390, 290),
        "has solar panels",
        source="llm",
        attrs={"attempt": 2, "accepted": True},
    )
    a = draw_regions(_page(), [rejected], geometry=_geom(), label_regions=False)
    b = draw_regions(_page(), [accepted], geometry=_geom(), label_regions=False)
    assert a.tobytes() != b.tobytes()


# ── Caption glyphs ────────────────────────────────────────────────────────


def test_safe_text_transliterates_glyphs_the_default_font_lacks():
    assert safe_text("attempt 1 ✗") == "attempt 1 x"
    assert safe_text("verify ✓ ok") == "verify ok ok"
    assert safe_text("a — b … c") == "a - b ... c"


def test_safe_text_keeps_the_separator_the_default_font_does_have():
    # U+00B7 renders fine, so replacing it would only make captions uglier.
    assert safe_text("has faces · 10 PASS · cv") == "has faces · 10 PASS · cv"


def test_a_caption_with_a_missing_glyph_draws_without_a_notdef_box():
    """The ✗ in a rejected-attempt caption must not reach the font.

    Pillow does not raise on a missing glyph — it draws a .notdef box, which
    in a review picture reads as a rendering bug rather than as a mark. So the
    assertion is behavioural: the ✗ caption must render identically to the
    caption that already spells it ``x``.
    """
    region = _box()
    with_symbol = draw_regions(_page(), [region], geometry=_geom(), labels=["hit ✗"])
    with_ascii = draw_regions(_page(), [region], geometry=_geom(), labels=["hit x"])
    assert with_symbol.tobytes() == with_ascii.tobytes()


# ── Helpers ───────────────────────────────────────────────────────────────


def test_annotate_to_jpeg_returns_decodable_bytes_of_the_right_size():
    data = annotate_to_jpeg(_page(), [_box(), _polygon()], geometry=_geom())
    assert data[:2] == b"\xff\xd8"
    decoded = Image.open(io.BytesIO(data))
    assert decoded.size == (400, 300)


def test_regions_from_json_round_trips_and_filters_by_page():
    payload = [_box().as_dict(), dict(_polygon().as_dict(), page=2)]
    every = regions_from_json(payload)
    assert [r.label for r in every] == ["has faces", "Notice to Owner"]
    assert [r.page for r in regions_from_json(payload, page=2)] == [2]
    assert regions_from_json(payload, page=9) == []


def test_bytes_and_ndarray_inputs_are_accepted():
    buf = io.BytesIO()
    _page().save(buf, format="PNG")
    from_bytes = draw_regions(buf.getvalue(), [_box()], geometry=_geom())
    assert from_bytes.size == (400, 300)

    np = pytest.importorskip("numpy")
    array = np.full((300, 400, 3), 255, dtype=np.uint8)
    from_array = draw_regions(array, [_box()], geometry=_geom())
    assert from_array.size == (400, 300)


def test_an_empty_region_list_returns_the_page_unchanged():
    out = draw_regions(_page(), [], geometry=_geom())
    assert out.tobytes() == _page().tobytes()


def test_a_degenerate_region_is_skipped_rather_than_crashing():
    degenerate = Region(page=0, kind="polygon", points=[(5, 5)], label="x", source="cv")
    out = draw_regions(_page(), [degenerate], geometry=_geom(), label_regions=False)
    assert out.tobytes() == _page().tobytes()
