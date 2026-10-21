"""Ruleset files, refs, validation, content hash, seed bindings, the cache."""

from __future__ import annotations

import asyncio
import copy
import pathlib

import pytest

from rp_helpers import RULESETS_DIR, default_ruleset, seed, strip_shadows

from route_policy.rulesets.loader import (
    BINDINGS_FILE,
    RulesetFileError,
    load_bindings,
    load_directory,
    load_seed,
    resolve_refs,
)
from route_policy.rulesets.model import content_sha256
from route_policy.rulesets.store import RulesetStore
from route_policy.rulesets.validate import RulesetInvalid, compile_ruleset


def _doc() -> dict:
    return copy.deepcopy(default_ruleset().document)


def _errors(doc) -> list:
    with pytest.raises(RulesetInvalid) as info:
        compile_ruleset(doc)
    return info.value.errors


# -- the baked-in seed -----------------------------------------------------------


def test_the_baked_in_seed():
    s = seed()
    # Only the current `default` is baked in: versions 1 (phase 2, shadow)
    # and 2 (phase 3) live on in the database alone (test_db.py's upgrade
    # tests). `default-llm` (phase 4) and `default-signals` (phase 5) are the
    # drafts, both bound as `auto`'s shadows; default-llm's version 1 lives on
    # in the database likewise.
    assert sorted((r.id, r.version, r.mode) for r in s.compiled) == [
        ("default", 3, "enforce"), ("default-llm", 3, "shadow"), ("default-signals", 1, "shadow")]
    assert [(b.scope_model, b.scope_key_alias, b.ruleset_id, b.version, b.shadows) for b in s.bindings] == [
        ("auto", None, "default", 3, (("default-llm", 3), ("default-signals", 1)))]
    ruleset = default_ruleset()
    assert ruleset.routes == ["local-general", "local-code", "local-reasoning", "local-private"]
    assert ruleset.default_route == "local-general"
    assert [r.pin for r in s.compiled] == ["turn", "turn", "turn"]
    assert [(e.id, e.type, e.role, e.route) for e in ruleset.entries] == [
        ("privacy", "regex_text", "force", "local-private"),
        ("code-text", "regex_text", "vote", "local-code"),
        ("reasoning-text", "regex_text", "vote", "local-reasoning"),
        ("code-tools", "tool_calls", "vote", "local-code"),
        ("sticky-code", "cooldown", "vote", "local-code"),
    ]
    assert ruleset.combiner.type_name == "priority"
    # The refs are inlined (and kept as provenance).
    config = ruleset.document["evaluators"][0]["config"]
    assert config["patterns_ref"] == "patterns/privacy.yaml" and config["rule"]["name"] == "privacy_kw"


def test_content_hash_ignores_comments_and_tracks_content(tmp_path):
    for path in RULESETS_DIR.rglob("*.yaml"):
        target = tmp_path / path.relative_to(RULESETS_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    def hashes(directory):
        return {r.key: r.sha256 for r in load_directory(directory)}

    base = hashes(tmp_path)
    assert base == hashes(RULESETS_DIR) and set(base) == {("default", 3), ("default-llm", 3),
                                                          ("default-signals", 1)}
    ruleset = tmp_path / "default.yaml"
    ruleset.write_text("# a new comment\n" + ruleset.read_text(encoding="utf-8"), encoding="utf-8")
    assert hashes(tmp_path) == base
    # A pattern file is content of EVERY ruleset that references it.
    pattern = tmp_path / "patterns" / "code.yaml"
    pattern.write_text(pattern.read_text(encoding="utf-8").replace("  - '```'\n", ""), encoding="utf-8")
    changed = hashes(tmp_path)
    assert all(changed[k] != base[k] for k in base)
    # An example set is content of the ruleset that references it -- only that one.
    examples = tmp_path / "examples" / "routes.yaml"
    examples.write_text(examples.read_text(encoding="utf-8").replace("my script throws", "my script raises"),
                        encoding="utf-8")
    again = hashes(tmp_path)
    assert again[("default-signals", 1)] != changed[("default-signals", 1)]
    assert {k: again[k] for k in again if k[0] != "default-signals"} == {
        k: changed[k] for k in changed if k[0] != "default-signals"}


def test_hash_is_over_the_document_not_a_pydantic_dump():
    doc = _doc()
    assert compile_ruleset(doc).sha256 == content_sha256(doc)
    assert "role" not in doc["evaluators"][1]   # defaults are not written into the stored document


# -- refs ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["../config.yaml", "/etc/passwd", "C:/x.yaml", "patterns/../../x.yaml",
                                 "..\\x.yaml", "", 3])
def test_refs_cannot_leave_the_rulesets_directory(ref):
    doc = {"evaluators": [{"id": "x", "type": "regex_text", "config": {"patterns_ref": ref}}]}
    with pytest.raises(RulesetFileError):
        resolve_refs(doc, RULESETS_DIR)


def test_ref_errors():
    missing = {"evaluators": [{"id": "x", "type": "regex_text", "config": {"patterns_ref": "patterns/none.yaml"}}]}
    with pytest.raises(RulesetFileError, match="does not exist"):
        resolve_refs(missing, RULESETS_DIR)
    # Already resolved (a stored document posted back): the ref is not re-read.
    stored = _doc()
    assert resolve_refs(stored, RULESETS_DIR) == stored
    assert compile_ruleset(resolve_refs(stored, RULESETS_DIR)).sha256 == default_ruleset().sha256
    # Anything it does not recognise is left for validation.
    assert resolve_refs({"evaluators": "nope"}, RULESETS_DIR) == {"evaluators": "nope"}


def test_examples_ref_is_inlined_as_examples():
    (signals,) = [r for r in seed().compiled if r.id == "default-signals"]
    (topic,) = [e for e in signals.document["evaluators"] if e["id"] == "topic"]
    assert topic["config"]["examples_ref"] == "examples/routes.yaml"
    assert topic["config"]["examples"]["name"] == "routes_v1" and len(topic["config"]["examples"]["examples"]) >= 75
    # A stored document posted back keeps its examples: the ref is not re-read.
    stored = copy.deepcopy(signals.document)
    stored["evaluators"][7]["config"]["examples"]["examples"] = stored["evaluators"][7]["config"]["examples"][
        "examples"][:3]
    assert resolve_refs(stored, RULESETS_DIR) == stored


def _topic_doc(config: dict) -> dict:
    return {"id": "x", "version": 1, "mode": "shadow", "routes": ["local-general", "local-code"],
            "default_route": "local-general",
            "evaluators": [{"id": "topic", "type": "nearest_examples",
                            "config": {"model": "intfloat/multilingual-e5-small", **config}}]}


def test_examples_ref_errors(tmp_path):
    with pytest.raises(RulesetFileError, match="does not exist"):
        resolve_refs(_topic_doc({"examples_ref": "examples/none.yaml"}), RULESETS_DIR)
    with pytest.raises(RulesetFileError, match="inside the rulesets directory"):
        resolve_refs(_topic_doc({"examples_ref": "../docker-compose.route-policy.yml"}), RULESETS_DIR)
    # A broken example file is a ruleset error, reported where it is.
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "bad.yaml").write_text(
        "name: bad\nexamples:\n  - {id: a, route: local-code, text: one}\n"
        "  - {id: a, route: local-code, text: two}\n", encoding="utf-8")
    with pytest.raises(RulesetInvalid) as info:
        compile_ruleset(resolve_refs(_topic_doc({"examples_ref": "examples/bad.yaml"}), tmp_path))
    joined = "\n".join(info.value.errors)
    assert "evaluators[0] (topic).config.examples.examples" in joined and "used twice" in joined


def test_duplicate_versions_and_bad_files(tmp_path):
    (tmp_path / "patterns").mkdir()
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text("id: x\nversion: 1\n", encoding="utf-8")
    with pytest.raises(RulesetFileError, match="also in"):
        load_directory(tmp_path)
    (tmp_path / "b.yaml").write_text("- a list\n", encoding="utf-8")
    with pytest.raises(RulesetFileError, match="one mapping"):
        load_directory(tmp_path)
    (tmp_path / "b.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(RulesetFileError, match="not valid YAML"):
        load_directory(tmp_path)


# -- validation ------------------------------------------------------------------------


def test_validation_reports_every_problem():
    doc = _doc()
    doc["default_route"] = "nowhere"
    doc["evaluators"][1]["vote"]["route"] = "claude-opus-5-5"
    doc["evaluators"][2]["type"] = "telepathy"
    doc["evaluators"][3]["config"]["patern"] = "typo"
    doc["evaluators"][4]["config"]["relevant_if"] = ["code-tools", "ghost"]
    errors = _errors(doc)
    joined = "\n".join(errors)
    assert "default_route: 'nowhere' is not one of routes" in joined
    assert "evaluators[1] (code-text).vote.route: 'claude-opus-5-5' is not one of routes" in joined
    assert "unknown evaluator type 'telepathy'" in joined
    assert "evaluators[3] (code-tools).config.patern" in joined
    # code-tools is declared but invalid; ghost is not declared at all.
    assert "relevant_if names 'code-tools', which is invalid" in joined
    doc = _doc()
    doc["evaluators"][4]["config"]["relevant_if"] = ["code-tools", "ghost"]
    assert "relevant_if names 'ghost', which is not an evaluator of this ruleset" in "\n".join(_errors(doc))


@pytest.mark.parametrize("change, expected", [
    (lambda d: d.update(mode="yolo"), "mode"),
    (lambda d: d.update(pin="message"), "pin"),
    (lambda d: d.update(pin=True), "pin"),
    (lambda d: d.update(version=0), "version"),
    (lambda d: d.update(id="Bad Id"), "id"),
    (lambda d: d.update(routes=[]), "routes"),
    (lambda d: d.update(routes=["local-general", "local-general"], default_route="local-general")
     or d.update(evaluators=[]), "listed twice"),
    (lambda d: d.update(surprise=1), "surprise"),
    (lambda d: d["evaluators"].append(dict(d["evaluators"][0])), "already used"),
    (lambda d: d["evaluators"][1]["vote"].pop("route"), "required for a regex_text"),
    (lambda d: d["evaluators"][0].update(role="boss"), "role"),
    (lambda d: d.update(combiner={"type": "majority"}), "unknown combiner"),
    (lambda d: d.update(combiner={"type": "weighted", "threshold": -1}), "combiner.threshold"),
    (lambda d: d.update(combiner={"type": "priority", "threshold": 1}), "combiner.threshold"),
])
def test_invalid_documents(change, expected):
    doc = _doc()
    change(doc)
    assert expected in "\n".join(_errors(doc))


def test_pin_is_optional_and_part_of_the_content():
    doc = _doc()
    assert doc["pin"] == "turn" and compile_ruleset(doc).pin == "turn"
    unpinned = _doc()
    del unpinned["pin"]
    assert compile_ruleset(unpinned).pin is None
    assert compile_ruleset({**unpinned, "pin": None}).pin is None
    # A different pin is different content: a new version, like any edit.
    assert content_sha256(unpinned) != content_sha256(doc)


def test_weighted_ruleset_compiles():
    doc = _doc()
    doc["combiner"] = {"type": "weighted", "threshold": 0.7}
    assert compile_ruleset(doc).combiner.config.threshold == 0.7


# -- seed bindings ------------------------------------------------------------------


def _bindings(tmp_path: pathlib.Path, text: str):
    (tmp_path / BINDINGS_FILE).write_text(text, encoding="utf-8")
    return load_bindings(tmp_path, load_directory(RULESETS_DIR))


def test_seed_bindings(tmp_path):
    (b,) = _bindings(tmp_path, "- {model: auto, ruleset: default}\n")
    assert (b.scope_model, b.scope_key_alias, b.version, b.shadows) == ("auto", None, 3, ())
    (b,) = _bindings(tmp_path, "- {model: auto, key_alias: tester, ruleset: default, version: 3}\n")
    assert b.scope_key_alias == "tester"
    (b,) = _bindings(tmp_path, "- {model: auto, ruleset: default, shadow: [default-llm]}\n")
    assert b.shadows == (("default-llm", 3),)
    (b,) = _bindings(tmp_path, "- {model: auto, ruleset: default, shadow: [{ruleset: default-llm, version: 3}]}\n")
    assert b.shadows == (("default-llm", 3),)
    (b,) = _bindings(tmp_path, "- {model: auto, ruleset: default, shadow: []}\n")
    assert b.shadows == ()
    (b,) = _bindings(tmp_path, "- {model: auto, ruleset: default, shadow: [default-llm, default-signals]}\n")
    assert b.shadows == (("default-llm", 3), ("default-signals", 1))
    for bad, match in [
        ("- {model: auto, ruleset: ghost}\n", "no ruleset 'ghost'"),
        ("- {model: auto, ruleset: default, version: 2}\n", "not baked in"),
        ("- {model: auto, ruleset: default, version: 9}\n", "not baked in"),
        ("- {model: auto, ruleset: default}\n- {model: auto, ruleset: default}\n", "bound twice"),
        ("- {model: auto, ruleset: default, extra: 1}\n", "expected"),
        ("{model: auto}\n", "a list"),
        # The seed rules: an enforce primary, shadow-mode shadows.
        ("- {model: auto, ruleset: default-llm}\n", "primary ruleset must be `enforce`"),
        ("- {model: auto, ruleset: default, shadow: [default]}\n", "a shadow slot takes a `shadow` ruleset"),
        ("- {model: auto, ruleset: default, shadow: [default-llm, default-llm]}\n", "listed twice"),
        ("- {model: auto, ruleset: default, shadow: [ghost]}\n", "no ruleset 'ghost'"),
        ("- {model: auto, ruleset: default, shadow: [{ruleset: default-llm, version: 7}]}\n", "not baked in"),
        ("- {model: auto, ruleset: default, shadow: [{ruleset: default-llm, pin: 1}]}\n", "expected a ruleset id"),
        ("- {model: auto, ruleset: default, shadow: default-llm}\n", "a list of ruleset ids"),
    ]:
        with pytest.raises(RulesetFileError, match=match):
            _bindings(tmp_path, bad)


def test_load_seed_refuses_an_invalid_ruleset(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nversion: 1\nmode: shadow\nroutes: [a]\ndefault_route: b\n", encoding="utf-8")
    with pytest.raises(RulesetInvalid, match="bad@1"):
        load_seed(tmp_path)


# -- the cache (no database) ------------------------------------------------------------


def test_store_resolves_bindings_by_model_and_key_alias():
    s = seed()
    store = RulesetStore(None)
    for r in s.compiled:
        store.add(r)
    from route_policy.rulesets.loader import Binding

    store.set_bindings([Binding("auto", None, "default", 3, (("default-llm", 3),)),
                        Binding("auto", "tester", "default", 3)])
    assert store.binding_for("auto", None).scope_key_alias is None
    assert store.binding_for("auto", "tester").scope_key_alias == "tester"
    assert store.binding_for("auto", "someone").scope_key_alias is None
    assert store.binding_for("local-code", None) is None
    resolved = asyncio.run(store.resolve("auto", None))
    assert resolved.primary.id == "default" and resolved.shadows == (("default-llm", 3),)
    assert [r.id for r in asyncio.run(store.shadow_rulesets(resolved))] == ["default-llm"]
    assert asyncio.run(store.resolve("auto", "tester")).shadows == ()
    assert asyncio.run(store.resolve("gpt-4", None)) is None
    assert asyncio.run(store.get("default")).version == 3
    assert asyncio.run(store.get("default", 2)) is None   # not baked in, and no database to ask
