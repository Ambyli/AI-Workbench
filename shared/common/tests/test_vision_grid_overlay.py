"""The labelled coordinate grid: same frame in, lines where the numbers say.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package common \\
        python -m pytest shared/common/tests/test_vision_grid_overlay.py -q -p no:cacheprovider
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from common.vision import draw_grid_overlay


def _white(width=400, height=300):
    return Image.new("RGB", (width, height), "white")


def test_output_is_the_same_size_and_rgb():
    out = draw_grid_overlay(_white())
    assert out.size == (400, 300) and out.mode == "RGB"


def test_lines_land_where_the_grid_says():
    """Grid 500 on a 400-px-wide image is column 200; 500 on 300 px is row 150."""
    out = np.asarray(draw_grid_overlay(_white()))
    on_vertical = out[80, 200]        # x = 500, away from any label tab
    on_horizontal = out[150, 120]     # y = 500
    off_grid = out[75, 175]           # between lines, between labels
    assert on_vertical[0] > on_vertical[1] + 40      # red over white
    assert on_horizontal[0] > on_horizontal[1] + 40
    assert tuple(off_grid) == (255, 255, 255)        # untouched


def test_labels_sit_along_every_edge():
    """Something is drawn in the label bands on all four edges, nothing
    (but the lines) in the middle of a blank image."""
    out = np.asarray(draw_grid_overlay(_white())).astype(int)
    changed = np.abs(out - 255).sum(axis=2) > 0
    assert changed[0:12, :].any() and changed[-12:, :].any()     # top, bottom
    assert changed[:, 0:12].any() and changed[:, -12:].any()     # left, right
    # A region clear of lines and edges stays blank: rows 35-55 sit between
    # the y=100 line (row 30) and the y=200 line (row 60); cols 130-155
    # between the x=300 line (col 120) and the x=400 line (col 160).
    assert not changed[35:55, 130:155].any()


def test_numpy_rgb_input_is_accepted():
    arr = np.full((300, 400, 3), 255, dtype=np.uint8)
    out = draw_grid_overlay(arr)
    assert isinstance(out, Image.Image) and out.size == (400, 300)
    assert tuple(arr[80, 200]) == (255, 255, 255)    # the input was not modified


def test_step_controls_line_count():
    coarse = np.asarray(draw_grid_overlay(_white(), step=250)).astype(int)
    fine = np.asarray(draw_grid_overlay(_white(), step=50)).astype(int)
    row = 80  # clear of the label bands
    assert (np.abs(fine[row] - 255).sum(axis=1) > 0).sum() > (np.abs(coarse[row] - 255).sum(axis=1) > 0).sum()


def test_degenerate_image_is_returned_unchanged():
    out = draw_grid_overlay(Image.new("RGB", (1, 1), "white"))
    assert out.size == (1, 1)


@pytest.mark.parametrize("size", [(1000, 750), (750, 1000), (1000, 1000), (600, 800)])
def test_any_aspect_ratio(size):
    out = draw_grid_overlay(Image.new("RGB", size, (40, 40, 40)))
    assert out.size == size
