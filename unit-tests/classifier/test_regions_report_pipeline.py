"""regions_report.py --local --pipeline utility-bill, end to end, with a scripted model.

The amount-due locator is the one part of the region report that cannot be
checked by ``--local`` alone: its only criterion is ``llm``, so with no vision
model it errors and every check is skipped. This test closes that gap. It runs
the script's own ``main()`` — collection parsing off, ``LocalTransport``, the
real FastAPI app with its worker pool, queue and artifact store, the real
enforcement loop (grid, refine, verify), the artifact download, the drawing,
the zoom, the checks and the HTML — with ONE thing replaced: the vision model,
at the bare transport ``llm.client._send``.

The scripted model is not a canned reply list. It reads each prompt to decide
what it is being asked, and answers the way a model that can see the bill
would, from the hand-measured truth in ``regions_expected.json``
(``utility_bill.jpeg``'s header line, ``Amount Due: $80.49``, at
[550, 70, 698, 85] on the upright page's 0-1000 grid):

    scoring  "CRITERION: ..."           → presence 9 (the loop's gate)
    ask      the gridded upright page   → a loose coarse box around the line
    refine   the zoomed crop            → the line, on THE CROP'S OWN grid —
                                          computed from the window the refine
                                          pass must have cut for the coarse
                                          box (checked against the crop's
                                          aspect ratio), not hard-coded
    verify   the crop alone             → 9 (or a scripted rejection)

So a box that lands on the line proves the whole mapping chain: EXIF
orientation (the file is 5712x4284 with orientation 6; the page is read
upright at 4284x5712), grid → window → page grid → original pixels, the
verify crop, the report's drawing and zoom, and the IoU check against the
expectations file.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_regions_report_pipeline.py -q -p no:cacheprovider
"""

from __future__ import annotations

import base64
import json
import os
import pathlib

import numpy as np
import pytest

# --local runs the app's lifespan, which opens the classifier's Postgres pool.
pytestmark = pytest.mark.postgres

HERE = pathlib.Path(__file__).resolve().parent
BILL = HERE / "documents" / "utility_bill.jpeg"
EXPECTATIONS = HERE / "regions_expected.json"

GRID = 1000.0


def _truth() -> tuple[list[float], list[float]]:
    """The two hand-measured amount-due lines on utility_bill.jpeg, from the
    expectations file the report itself checks against."""
    expected = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
    boxes = expected["pipeline: utility bill — utility_bill.jpeg"]["amount_due_boxes_grid"]
    header = min(boxes, key=lambda b: b[1])  # the one at the top of the page
    stub = max(boxes, key=lambda b: b[1])
    return header, stub


def _upright_size() -> tuple[int, int]:
    from PIL import Image, ImageOps

    with Image.open(BILL) as image:
        return ImageOps.exif_transpose(image).size


# ---------------------------------------------------------------------------
# The scripted model
# ---------------------------------------------------------------------------


def _content(obj: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


def _user_text(prompt: dict) -> str:
    return next(
        part["text"] for part in prompt["messages"][1]["content"] if part.get("type") == "text"
    )


def _image(prompt: dict):
    import cv2

    for part in prompt["messages"][1]["content"]:
        if part.get("type") == "image_url":
            b64 = part["image_url"]["url"].split("base64,", 1)[1]
            return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)
    return None


def _kind(prompt: dict) -> str:
    text = _user_text(prompt)
    if text.startswith("Is this visible in the attached image"):
        return "verify"
    if text.startswith("Locate this feature"):
        return "refine" if "zoomed-in crop" in text else "ask"
    if "CRITERION:" in text:
        return "score"
    raise AssertionError(f"unrecognised prompt: {text[:120]!r}")


def _window_for(coarse: list[float]) -> list[float]:
    """The refine window the service must cut for ``coarse``, from the two
    configured knobs (CLASSIFIER_LLM_BBOX_REFINE_ZOOM / _MIN_SPAN) and the
    rule API.md states: zoom x the box on each axis, centred, at least
    min_span of the page, clamped to the page. Written out here rather than
    calling llm.boxes.refine_window, so a change to the service's window is
    caught by the crop-aspect check below instead of silently agreed with."""
    from config import LLM_BBOX_REFINE_MIN_SPAN, LLM_BBOX_REFINE_ZOOM

    x1, y1, x2, y2 = coarse
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w = max((x2 - x1) * LLM_BBOX_REFINE_ZOOM, GRID * LLM_BBOX_REFINE_MIN_SPAN)
    h = max((y2 - y1) * LLM_BBOX_REFINE_ZOOM, GRID * LLM_BBOX_REFINE_MIN_SPAN)
    return [max(0.0, cx - w / 2), max(0.0, cy - h / 2),
            min(GRID, cx + w / 2), min(GRID, cy + h / 2)]


class ScriptedBillModel:
    """Answers the four prompt types from a per-attempt plan.

    ``plan`` is one dict per enforcement-loop attempt:
      coarse  the ask's answer on the gridded page (page grid)
      target  where the refine answer should land, on the PAGE grid; the
              model converts it into the crop's own grid
      verify  the verify call's 1-10
    """

    def __init__(self, plan: list[dict], page_size: tuple[int, int]):
        self.plan = plan
        self.page_w, self.page_h = page_size
        self.kinds: list[str] = []
        self.prompts: list[dict] = []
        self.windows: list[list[float]] = []
        self.verify_crops: list[tuple[int, int]] = []
        self._attempt = -1

    async def __call__(self, prompt: dict) -> dict:
        kind = _kind(prompt)
        self.kinds.append(kind)
        self.prompts.append(prompt)
        image = _image(prompt)

        if kind == "score":
            return _content({"score": 9, "verdict": "PASS", "confidence": 90,
                             "reason": "The header reads 'Amount Due: $80.49'."})

        if kind == "ask":
            self._attempt += 1
            step = self.plan[self._attempt]
            # The ask sees the UPRIGHT working page with the grid on it. A
            # decoder that ignored EXIF would hand over a landscape page.
            assert image is not None and image.shape[0] > image.shape[1], image.shape
            assert "coordinate grid is drawn over the image" in _user_text(prompt)
            return _content({"bbox": step["coarse"], "confidence": 70,
                             "reason": "the amount due text in the header"})

        step = self.plan[self._attempt]
        if kind == "refine":
            window = _window_for(step["coarse"])
            self.windows.append(window)
            # The crop the service sent must be that window of the upright
            # page (brought to the working size, aspect kept).
            want = ((window[2] - window[0]) * self.page_w) / ((window[3] - window[1]) * self.page_h)
            got = image.shape[1] / image.shape[0]
            assert abs(got - want) / want < 0.02, (got, want, window)
            wx1, wy1, wx2, wy2 = window
            tx1, ty1, tx2, ty2 = step["target"]
            on_crop = [
                (tx1 - wx1) / (wx2 - wx1) * GRID, (ty1 - wy1) / (wy2 - wy1) * GRID,
                (tx2 - wx1) / (wx2 - wx1) * GRID, (ty2 - wy1) / (wy2 - wy1) * GRID,
            ]
            return _content({"bbox": [round(v, 1) for v in on_crop], "confidence": 85,
                             "reason": "the 'Amount Due' line"})

        # verify — the crop alone, cut from the original upright page.
        self.verify_crops.append((image.shape[1], image.shape[0]))
        return _content({"score": step["verify"],
                         "reason": "Amount Due: $80.49" if step["verify"] >= 7
                         else "a paragraph of usage history, no amount due"})


# ---------------------------------------------------------------------------
# Running the script
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_environ():
    """main() loads .env and LocalTransport points CLASSIFIER_DATA_DIR & co. at its work
    directory; neither may leak into the rest of the session."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _run(monkeypatch, tmp_path, plan):
    import regions_report
    from llm import client as llm_client

    page_size = _upright_size()
    model = ScriptedBillModel(plan, page_size)
    monkeypatch.setattr(llm_client, "_send", model)
    # Only the bill whose truth the model is scripted from.
    monkeypatch.setattr(regions_report, "UTILITY_BILL_FIXTURES", (BILL,))

    drawn: list[tuple[list, list[str]]] = []
    real_annotate = regions_report.annotate_to_jpeg

    def recording_annotate(base, regions, **kwargs):
        drawn.append((list(regions), list(kwargs.get("labels") or [])))
        return real_annotate(base, regions, **kwargs)

    monkeypatch.setattr(regions_report, "annotate_to_jpeg", recording_annotate)

    out = tmp_path / "report"
    code = regions_report.main(["--local", "--pipeline", "utility-bill", "--out", str(out)])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    return code, out, summary, model, drawn, page_size


def _px(box_grid, page_size):
    w, h = page_size
    return [box_grid[0] * w / GRID, box_grid[1] * h / GRID,
            box_grid[2] * w / GRID, box_grid[3] * h / GRID]


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


HEADER, _STUB = _truth()
# A loose first answer around the header line — the size of miss the grounding
# measurement saw on the bare ask (tens of grid units on each axis).
COARSE_HEADER = [530.0, 55.0, 725.0, 108.0]


def test_amount_due_located_on_first_attempt(monkeypatch, tmp_path, capsys, restore_environ):
    code, out, summary, model, drawn, page_size = _run(
        monkeypatch, tmp_path,
        [{"coarse": COARSE_HEADER, "target": HEADER, "verify": 9}],
    )
    stdout = capsys.readouterr().out

    # The run, as the user sees it.
    assert code == 0, stdout
    # The tally line ends right there when nothing was skipped.
    assert "3/3 expectations met\n" in stdout
    assert "amount due located" in stdout

    # Exactly one scoring call, then ask → refine → verify.
    assert model.kinds == ["score", "ask", "refine", "verify"]

    [item] = summary["items"]
    assert item["phase"] == "completed"
    [pipe] = summary["pipelines"]
    assert pipe["scoring_calls"] == 1 and pipe["calls"] == 3
    accepted = pipe["accepted"]
    assert accepted is not None and accepted["attempt"] == 1 and accepted["accepted"] is True
    assert accepted["refined"] is True and accepted["verify_score"] == 9
    assert accepted["coarse_bbox_grid"] == COARSE_HEADER
    assert accepted["refine_window_grid"] == pytest.approx(model.windows[0], abs=0.1)

    # The refine answer mapped back onto the header line, on the page grid,
    # and bbox_px is that box in UPRIGHT original pixels (4284 x 5712).
    assert page_size == (4284, 5712)
    assert accepted["bbox_grid"] == pytest.approx(HEADER, abs=0.2)
    assert accepted["bbox_px"] == pytest.approx(_px(HEADER, page_size), abs=2.0)

    # The verify crop was that box plus CLASSIFIER_LLM_BBOX_CROP_PAD a side,
    # out of the original pixels (not the ≤1000-px working copy).
    from config import LLM_BBOX_CROP_PAD

    bx1, by1, bx2, by2 = accepted["bbox_px"]
    want_w = (bx2 - bx1) * (1 + 2 * LLM_BBOX_CROP_PAD)
    want_h = (by2 - by1) * (1 + 2 * LLM_BBOX_CROP_PAD)
    got_w, got_h = model.verify_crops[0]
    assert abs(got_w - want_w) <= 3 and abs(got_h - want_h) <= 3, (model.verify_crops, want_w, want_h)

    # The checks: box accepted, IoU with the header line well over 0.25.
    checks = {c["target"]: c for c in pipe["checks"]}
    assert checks["a box was accepted"]["ok"] is True
    assert checks["the box sits on an amount-due line"]["ok"] is True
    assert not any(c.get("skipped") for c in pipe["checks"] + item["checks"])
    assert pipe["best_iou"] >= 0.9

    # The report's files.
    case_dir = out / pipe["slug"]
    assert (out / "index.html").is_file()
    assert (case_dir / "p0.annotated.jpg").is_file()
    assert (case_dir / "amount_due_zoom.jpg").is_file()
    assert pipe["zoom"] == "amount_due_zoom.jpg"

    # The drawing: one llm region, accepted, exactly the stored bbox_px.
    [(regions, labels)] = drawn
    llm = [r for r in regions if r.source == "llm"]
    assert len(llm) == 1 and llm[0].attrs["accepted"] is True
    (px1, py1), (px2, py2) = llm[0].points[0], llm[0].points[-1]
    assert [px1, py1, px2, py2] == pytest.approx(accepted["bbox_px"], abs=0.01)
    assert "attempt 1 verify=9" in labels[0] and "refined" in labels[0]

    _assert_zoom_contains_box(case_dir / "amount_due_zoom.jpg", pipe["zoom_window_px"],
                              accepted["bbox_px"], page_size)

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Where is the amount due? — utility_bill.jpeg" in html
    assert "1 scoring + 3 loop model call(s)" in html
    assert "<h3>Attempts</h3>" in html and "<span class='ok'>accepted</span>" in html


def _assert_zoom_contains_box(path, window, box, page_size):
    """The zoom is the UPRIGHT page cut at ``window`` with the box drawn on it."""
    from PIL import Image, ImageOps

    from regions_report import ZOOM_PAD

    w, h = page_size
    x1, y1, x2, y2 = box
    left, top, right, bottom = window
    # The window is the box plus ZOOM_PAD of the page a side, clamped.
    assert left == int(max(0, x1 - w * ZOOM_PAD)) and top == int(max(0, y1 - h * ZOOM_PAD))
    assert right == int(min(w, x2 + w * ZOOM_PAD)) and bottom == int(min(h, y2 + h * ZOOM_PAD))
    assert left <= x1 < x2 <= right and top <= y1 < y2 <= bottom

    with Image.open(path) as zoom:
        zoom = zoom.convert("RGB")
    assert zoom.size == (right - left, bottom - top)

    # Its pixels are the upright page's, away from the drawn box …
    with Image.open(BILL) as raw:
        upright = ImageOps.exif_transpose(raw).convert("RGB").crop((left, top, right, bottom))
    corner = (0, 0, min(120, int(x1 - left) - 10), min(120, int(y1 - top) - 10))
    diff = np.abs(np.asarray(zoom.crop(corner), float) - np.asarray(upright.crop(corner), float))
    assert diff.mean() < 12, diff.mean()

    # … and the box is drawn where the box is: the green outline on its left
    # and top edges, just inside the rectangle.
    arr = np.asarray(zoom, int)
    bx1, by1, bx2, by2 = (int(round(v)) for v in (x1 - left, y1 - top, x2 - left, y2 - top))
    mid_y, mid_x = (by1 + by2) // 2, (bx1 + bx2) // 2
    for px, py in ((bx1 + 1, mid_y), (mid_x, by1 + 1), (bx2 - 2, mid_y), (mid_x, by2 - 2)):
        r, g, b = arr[py, px]
        assert g > 120 and r < 90 and g > b, (px, py, (r, g, b))


def test_rejected_first_attempt_is_reported_and_drawn(monkeypatch, tmp_path, capsys, restore_environ):
    # Attempt 1 points at the usage-history paragraph; the verifier rejects
    # its crop. Attempt 2 is the header line and is accepted.
    wrong_coarse = [120.0, 430.0, 380.0, 470.0]
    wrong_target = [150.0, 440.0, 350.0, 460.0]
    code, out, summary, model, drawn, page_size = _run(
        monkeypatch, tmp_path,
        [
            {"coarse": wrong_coarse, "target": wrong_target, "verify": 3},
            {"coarse": COARSE_HEADER, "target": HEADER, "verify": 9},
        ],
    )
    stdout = capsys.readouterr().out
    assert code == 0, stdout
    assert "3/3 expectations met" in stdout

    assert model.kinds == ["score", "ask", "refine", "verify", "ask", "refine", "verify"]
    # The second ask carries the first rejection as feedback.
    second_ask = _user_text(model.prompts[4])
    assert "Your previous answer(s) were rejected" in second_ask and "verify score 3" in second_ask

    [pipe] = summary["pipelines"]
    assert pipe["calls"] == 6
    first, second = pipe["attempts"]
    assert first["accepted"] is False and first["verify_score"] == 3
    assert "did not show" in first["reject"]
    assert first["bbox_grid"] == pytest.approx(wrong_target, abs=0.2)
    assert second["accepted"] is True and pipe["accepted"]["attempt"] == 2
    assert _iou(pipe["accepted"]["bbox_grid"], HEADER) > 0.9

    # Both drawn: the accepted one from its stored bbox_px, the rejected one
    # from what the model said, labelled as rejected.
    [(regions, labels)] = drawn
    llm = [(r, label) for r, label in zip(regions, labels) if r.source == "llm"]
    assert len(llm) == 2
    rejected = next((r, lab) for r, lab in llm if r.attrs["accepted"] is False)
    kept = next((r, lab) for r, lab in llm if r.attrs["accepted"] is True)
    (rx1, ry1), (rx2, ry2) = rejected[0].points[0], rejected[0].points[-1]
    assert [rx1, ry1, rx2, ry2] == pytest.approx(_px(first["bbox_grid"], page_size), abs=1.0)
    assert "attempt 1 ✗" in rejected[1]
    assert "attempt 2 verify=9" in kept[1]

    # The attempts table lists both, rejected with the reason.
    html = (out / "index.html").read_text(encoding="utf-8")
    table = html.split("<h3>Attempts</h3>", 1)[1].split("</table>", 1)[0]
    assert table.count("<tr>") == 3  # header + two attempts
    assert "<span class='bad'>rejected</span>" in table and "<span class='ok'>accepted</span>" in table
    assert "did not show" in table
    assert "[120, 430, 380, 470]" in table and "[530, 55, 725, 108]" in table
    assert (out / pipe["slug"] / "amount_due_zoom.jpg").is_file()
