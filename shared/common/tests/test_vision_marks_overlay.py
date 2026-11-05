"""Numbered marks: each box outlined where its numbers say, its tab beside it.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package common \\
        python -m pytest shared/common/tests/test_vision_marks_overlay.py -q -p no:cacheprovider
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from common.vision import MARK_COLOURS, draw_marks_overlay


def _white(width=400, height=300):
    return Image.new("RGB", (width, height), "white")


def _changed(out) -> np.ndarray:
    return np.abs(np.asarray(out).astype(int) - 255).sum(axis=2) > 0


def test_output_is_the_same_size_and_rgb_and_the_input_is_untouched():
    image = _white()
    out = draw_marks_overlay(image, [[100, 100, 300, 130]])
    assert out.size == (400, 300) and out.mode == "RGB"
    assert out is not image
    assert np.asarray(image).min() == 255           # the input was not drawn on


def test_the_outline_lands_on_the_box_and_nowhere_inside_it():
    out = np.asarray(draw_marks_overlay(_white(), [[100, 100, 300, 160]], line_width=2))
    blue = MARK_COLOURS[0]
    assert tuple(out[100, 200]) == blue              # top edge
    assert tuple(out[159, 200]) == blue              # bottom edge (width 2, inward)
    assert tuple(out[130, 300]) == blue              # right edge
    assert tuple(out[130, 200]) == (255, 255, 255)   # the inside stays readable


def test_the_tab_sits_left_of_the_box_and_holds_white_digits():
    out = draw_marks_overlay(_white(), [[200, 100, 350, 130]], font_size=14)
    changed = _changed(out)
    # Something is drawn just left of the box, at its height…
    assert changed[100:131, 150:199].any()
    # …in the box's colour, with white digits inside the tab.
    arr = np.asarray(out)
    tab = arr[100:131, 150:199].reshape(-1, 3)
    assert any(tuple(px) == MARK_COLOURS[0] for px in tab)
    # Above and below the line nothing at all is drawn.
    assert not changed[0:90, :].any() and not changed[140:, :].any()


def test_a_box_at_the_left_edge_gets_its_tab_inside():
    out = draw_marks_overlay(_white(), [[0, 100, 200, 130]], font_size=14)
    changed = _changed(out)
    assert changed[100:131, 1:40].any()             # inside the left end of the box
    assert not changed[:95, :].any()                # never pushed above it


def test_colours_cycle_and_labels_default_to_one_to_n():
    boxes = [[100, 20 + 40 * i, 300, 45 + 40 * i] for i in range(len(MARK_COLOURS) + 1)]
    out = np.asarray(draw_marks_overlay(_white(400, 320), boxes))
    # Each box's top edge (away from its tab) is in its own colour, the
    # palette restarting after the last one.
    for i, box in enumerate(boxes):
        assert tuple(out[box[1], 250]) == MARK_COLOURS[i % len(MARK_COLOURS)]


def test_custom_labels_must_match_the_boxes():
    draw_marks_overlay(_white(), [[10, 10, 50, 30]], labels=["A"])
    with pytest.raises(ValueError):
        draw_marks_overlay(_white(), [[10, 10, 50, 30]], labels=["1", "2"])


def test_a_box_partly_outside_is_clipped_and_numpy_input_works():
    arr = np.full((300, 400, 3), 255, dtype=np.uint8)
    out = draw_marks_overlay(arr, [[300, 250, 600, 400]])
    assert isinstance(out, Image.Image) and out.size == (400, 300)
    assert _changed(out)[250:, 300:].any()
    assert tuple(arr[260, 350]) == (255, 255, 255)   # the input array is untouched


def test_no_boxes_is_a_copy():
    out = draw_marks_overlay(_white(), [])
    assert not _changed(out).any()
