"""utility_bill_reference_report.py --local, end to end, with a scripted model.

``--local`` alone proves the reference plumbing (create → ready → listed on
/assess → deleted) but not the guided call: with no vision model the llm
criterion errors. This test runs the script's own ``main()`` — the real app
in-process, its queue, the reference store and renderer, the guided scoring
path, the artifact download, the checks and the HTML — with ONE thing
replaced: the vision model, at ``llm.client._send``.

The scripted model answers by what it is shown: a scoring call carrying ONE
image (the baseline) gets 7, one carrying TWO (the reference composite and
the candidate) gets 10 — and that second call's text must carry the
reference's caption, PASS (10) with the spec's reason. So a +3 in the
report's comparison is only reachable if the reference actually reached the
prompt.

The spec is the real ``utility_bill_reference.json`` minus its text
criteria: OCR is off in this suite (conftest), so they would only FAIL and
blur the verdict the test asserts.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_utility_bill_reference_report.py -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

# --local runs the app's lifespan, which opens the classifier's Postgres pool.
pytestmark = pytest.mark.postgres

HERE = pathlib.Path(__file__).resolve().parent
SPEC = HERE / "utility_bill_reference.json"
CANDIDATE = HERE / "documents" / "utility_bill.jpeg"


def _content(obj: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


class ScriptedModel:
    """Scores ``baseline`` with no example in view, 10 with one."""

    def __init__(self, baseline: int = 7):
        self.baseline = baseline
        self.calls: list[tuple[int, list[str]]] = []   # (images, text parts)

    async def __call__(self, prompt: dict) -> dict:
        parts = prompt["messages"][1]["content"]
        images = sum(1 for p in parts if p.get("type") == "image_url")
        texts = [p["text"] for p in parts if p.get("type") == "text"]
        self.calls.append((images, texts))
        if images >= 2:
            return _content({"score": 10, "verdict": "PASS", "confidence": 95,
                             "reason": "I observe an account number, a billing period, kWh "
                                       "usage and an amount due, laid out like the reference. "
                                       "Therefore a utility bill is present."})
        verdict = "PASS" if self.baseline >= 7 else "MARGINAL" if self.baseline >= 4 else "FAIL"
        return _content({"score": self.baseline, "verdict": verdict, "confidence": 70,
                         "reason": "I observe a statement with an amount due. Therefore the "
                                   "criterion is judged without an example."})


@pytest.fixture
def restore_environ():
    """main() loads .env and LocalTransport points CLASSIFIER_DATA_DIR & co. at its work
    directory; neither may leak into the rest of the session."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _llm_only_spec(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str, dict]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["criteria"] = [c for c in spec["criteria"] if (c.get("type") or "llm") == "llm"]
    [criterion] = spec["criteria"]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path, criterion["name"], spec["reference"]["breakdown"][criterion["name"]]


def test_reference_guides_the_llm_criterion(monkeypatch, tmp_path, capsys, restore_environ):
    import utility_bill_reference_report as report
    from llm import client as llm_client

    model = ScriptedModel()
    monkeypatch.setattr(llm_client, "_send", model)
    spec, name, answer = _llm_only_spec(tmp_path)
    out = tmp_path / "report"

    code = report.main(["--local", str(CANDIDATE), "--expect", "PASS",
                        "--spec", str(spec), "--out", str(out)])
    stdout = capsys.readouterr().out
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))

    assert code == 0, stdout
    # ready + usable example, two jobs completed, the guided call, the verdict.
    assert "6/6 checks met\n" in stdout

    # Creating the reference cost no model call (answer key, region and
    # description all supplied); then one baseline call, one guided call.
    assert [images for images, _ in model.calls] == [1, 2]
    guided_text = "\n".join(model.calls[1][1])
    assert f"REFERENCE EXAMPLE — in this reference '{name}' was PASS (10)" in guided_text
    assert answer["reason"] in guided_text
    assert "CANDIDATE — score only this image" in guided_text

    ref = summary["reference"]
    assert ref["status"] == "ready" and ref["deleted"] == "deleted"
    assert ref["description_source"] == "caller"
    [criterion] = ref["criteria"]
    assert criterion["usable"] is True and criterion["region_source"] == "whole_page"
    composites = [f for f in ref["files"] if f.startswith("c.")]
    assert len(composites) == 1 and (out / "reference" / composites[0]).is_file()

    [cand] = summary["candidates"]
    assert cand["baseline"]["phase"] == cand["guided"]["phase"] == "completed"
    assert cand["guided"]["verdict"] == "PASS" and cand["guided"]["score"] == 10
    [row] = cand["comparison"]
    assert row["guided_by_reference"] is True
    assert (row["baseline"]["score"], row["guided"]["score"], row["delta"]) == (7, 10, 3)

    detail = cand["reference_detail"][name]
    assert detail["applied"] is True and detail["mode"] == "explicit"
    [example] = detail["examples"]
    assert example["reference_id"] == ref["reference_id"]
    assert example["expected"] == {"score": 10, "verdict": "PASS"}
    assert example["polarity"] == "pass"
    [call] = detail["calls"]
    assert call["images"] == 2 and call["score"] == 10 and call["error"] is None

    assert not any(c.get("skipped") for c in ref["checks"] + cand["checks"])

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Without vs with the reference" in html
    assert f'src="reference/{composites[0]}"' in html
    assert "yes — a utility bill" in html


def test_keep_reference_and_no_baseline(monkeypatch, tmp_path, capsys, restore_environ):
    import utility_bill_reference_report as report
    from llm import client as llm_client

    model = ScriptedModel()
    monkeypatch.setattr(llm_client, "_send", model)
    spec, _, _ = _llm_only_spec(tmp_path)
    out = tmp_path / "report"

    code = report.main(["--local", str(CANDIDATE), "--spec", str(spec), "--out", str(out),
                        "--keep-reference", "--no-baseline"])
    stdout = capsys.readouterr().out
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))

    # No --expect on an ad-hoc candidate: the verdict check is skipped, not failed.
    assert code == 0, stdout
    assert "(1 skipped)" in stdout
    assert "kept — DELETE it when done" in stdout
    assert [images for images, _ in model.calls] == [2]
    assert summary["reference"]["deleted"] is None
    [cand] = summary["candidates"]
    assert cand["baseline"] is None
    assert cand["comparison"][0]["delta"] is None


def test_spec_candidates_and_a_flipped_verdict(monkeypatch, tmp_path, capsys, restore_environ):
    """The K7 example: an internal label only the reference defines. With no
    candidates on the command line the spec's own are run, each with its own
    expectation, and ``expect_changed`` asserts the reference flipped it."""
    import utility_bill_reference_report as report
    from llm import client as llm_client

    model = ScriptedModel(baseline=1)
    monkeypatch.setattr(llm_client, "_send", model)
    spec = json.loads((HERE / "utility_bill_k7_reference.json").read_text(encoding="utf-8"))
    # Only the bill: the scripted model cannot tell an invoice from a bill.
    spec["candidates"] = [c for c in spec["candidates"] if c["document"].endswith("utility_bill.jpeg")]
    assert spec["candidates"][0]["expect_changed"] is True
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    out = tmp_path / "report"

    code = report.main(["--local", "--spec", str(path), "--out", str(out)])
    stdout = capsys.readouterr().out
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))

    assert code == 0, stdout
    assert "7/7 checks met\n" in stdout
    assert "without the reference: FAIL 1 — the reference CHANGED the verdict" in stdout
    [cand] = summary["candidates"]
    assert cand["document"].endswith("documents/utility_bill.jpeg")
    assert (cand["baseline"]["verdict"], cand["guided"]["verdict"]) == ("FAIL", "PASS")
    assert cand["changed"] is True and cand["expect_changed"] is True
    impact = next(c for c in cand["checks"] if c["kind"] == "impact")
    assert impact["ok"] is True and impact["actual"] == "FAIL → PASS"


def test_expect_changed_fails_when_the_reference_changes_nothing(
        monkeypatch, tmp_path, capsys, restore_environ):
    import utility_bill_reference_report as report
    from llm import client as llm_client

    monkeypatch.setattr(llm_client, "_send", ScriptedModel(baseline=9))
    spec = json.loads((HERE / "utility_bill_k7_reference.json").read_text(encoding="utf-8"))
    spec["candidates"] = spec["candidates"][:1]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    code = report.main(["--local", "--spec", str(path), "--out", str(tmp_path / "report")])
    stdout = capsys.readouterr().out

    assert code == 1, stdout
    assert "6/7 checks met" in stdout
    assert "same verdict either way" in stdout
