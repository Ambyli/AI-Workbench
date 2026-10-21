"""Model specs and load state (signals/runtime.py, models.yaml) and the
build-time fetcher (signals/fetch_models.py) -- no model is loaded."""

from __future__ import annotations

import hashlib
import pathlib
import re

import pytest
import yaml

from common.signals import TASK_MODELS

from signals import config
from signals.fetch_models import download, notice, source_url
from signals.runtime import FAILED, LOADING, READY, Models, _spec, load_models, load_specs, verify_files

MODELS_YAML = config.MODELS_FILE


def test_models_yaml_pins_every_lane_by_revision_and_sha256():
    specs = load_specs()
    assert list(specs) == list(config.LANES)
    assert set(TASK_MODELS.values()) == set(specs)                  # every model key the wire names
    for lane, spec in specs.items():
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{12}", spec.model_id), spec.model_id
        assert spec.model in spec.files
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in spec.files.values())
        if spec.url is None:
            assert re.fullmatch(r"[0-9a-f]{40}", spec.revision)
            assert spec.model_id == f"{spec.repo}@{spec.revision[:12]}"
            assert "README.md" in spec.files and "tokenizer.json" in spec.files   # the card carries the licence
    assert specs["pii"].repo == "gravitee-io/bert-small-pii-detection"
    assert specs["embed"].repo == "intfloat/multilingual-e5-small"
    assert specs["injection"].repo == "NeuralTrust/prompt-guard-oss-small"
    assert {"model.onnx", "model.onnx.data"} <= set(specs["injection"].files)     # external weights, same dir
    assert specs["language"].model_id == f"fasttext/lid.176@{specs['language'].files['lid.176.ftz'][:12]}"
    assert {s.licence for s in specs.values()} == {"Apache-2.0", "MIT", "CC-BY-SA-3.0"}
    assert "Share-Alike" in specs["language"].attribution


def test_models_yaml_carries_no_routing_threshold():
    text = MODELS_YAML.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    keys = set()

    def walk(node):
        if isinstance(node, dict):
            keys.update(node)
            for value in node.values():
                walk(value)

    walk(doc)
    assert not {k for k in keys if any(w in str(k) for w in ("threshold", "min_share", "min_score"))}


@pytest.mark.parametrize("patch,needle", [
    ({"files": {"model.onnx": "abc"}}, "sha256"),
    ({"url": "https://example.invalid/x"}, "exactly one of revision"),
    ({"model": "missing.onnx"}, "is not one of its files"),
    ({"files": {"../escape.onnx": "0" * 64}, "model": "../escape.onnx"}, "relative path"),
    ({"revision": "main"}, "40-hex"),
])
def test_a_bad_spec_is_refused(patch, needle):
    raw = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))["models"]["pii"]
    raw.update(patch)
    with pytest.raises(ValueError, match=needle):
        _spec("pii", raw)


def test_models_track_load_state_and_hide_unready_models():
    models = Models(load_specs())
    assert set(models.state.values()) == {LOADING} and not models.ready()
    models.set_ready("pii", "pii-model", 1.234)
    assert models.get("pii") == "pii-model" and models.get("embed") is None
    models.set_failed("embed", "OSError", 0.5)
    snap = models.snapshot()
    assert snap["state"]["pii"] == READY and snap["state"]["embed"] == FAILED
    assert snap["errors"] == {"embed": "OSError"} and snap["load_seconds"]["pii"] == 1.23


def test_load_models_warms_each_lane_and_records_a_failure_by_type_only():
    class Model:
        warmed = False

        def warm_up(self):
            self.warmed = True

    built = {}

    def loader(lane, spec):
        if lane == "injection":
            raise OSError("no such file: /models/injection/secret path")
        built[lane] = Model()
        return built[lane]

    models = Models(load_specs())
    load_models(models, loader)
    assert all(m.warmed for m in built.values()) and set(built) == {"pii", "embed", "language"}
    assert models.state["injection"] == FAILED and models.errors == {"injection": "OSError"}
    assert not models.ready()


def test_verify_files_reports_missing_and_mismatched_files(tmp_path):
    specs = {"pii": load_specs()["pii"]}
    (tmp_path / "pii").mkdir()
    (tmp_path / "pii" / "model.onnx").write_bytes(b"not the model")
    problems = verify_files(specs, tmp_path)
    assert "pii/model.onnx: sha256 mismatch" in problems
    assert "pii/tokenizer.json: missing" in problems


def test_the_fetcher_downloads_by_revision_and_refuses_a_wrong_hash(tmp_path):
    entry = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))["models"]
    pii, language = entry["pii"], entry["language"]
    assert source_url(pii, "model.onnx") == (
        f"https://huggingface.co/{pii['repo']}/resolve/{pii['revision']}/model.onnx")
    assert source_url(language, "lid.176.ftz") == language["url"]
    source = tmp_path / "source.bin"
    source.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()
    target = tmp_path / "out" / "lane" / "model.onnx"
    download(source.as_uri(), target, digest)
    assert target.read_bytes() == b"weights" and not target.with_name("model.onnx.part").exists()
    with pytest.raises(SystemExit, match="sha256"):
        download(source.as_uri(), tmp_path / "out" / "bad.onnx", "0" * 64)
    assert not (tmp_path / "out" / "bad.onnx").exists()


def test_notice_names_the_pin_and_carries_fasttexts_attribution():
    models = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))["models"]
    text = notice("language", models["language"])
    assert "licence: CC-BY-SA-3.0" in text and "Attribution:" in text and "fasttext.cc" in text
    pii = notice("pii", models["pii"])
    assert models["pii"]["revision"] in pii and "README.md" in pii and "Apache-2.0" in pii


def test_the_dockerfile_bakes_and_checks_what_models_yaml_pins():
    dockerfile = (pathlib.Path(config.MODELS_FILE).parent / "Dockerfile").read_text(encoding="utf-8")
    assert "fetch_models.py --models-file ai/signals/models.yaml --dest /models" in dockerfile
    assert "python -m signals.runtime --check" in dockerfile
    # The fetch layer must not depend on the service code (a code edit would re-download 1.2 GB).
    assert dockerfile.index("fetch_models.py --models-file") < dockerfile.index("COPY ai/signals/signals ")
