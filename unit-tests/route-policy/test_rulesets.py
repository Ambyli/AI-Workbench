"""Ruleset files, refs, validation, content hash, seed bindings, the cache."""

from __future__ import annotations

import asyncio
import copy
import pathlib

import pytest

from rp_helpers import RULESETS_DIR, default_ruleset, seed

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
    # Only the current version is baked in: version 1 (phase 2, shadow) lives
    # on in the database alone (test_db.py::test_the_phase_3_upgrade_...).
    assert [(r.id, r.version, r.mode) for r in s.compiled] == [("default", 2, "enforce")]
    assert [(b.scope_model, b.scope_key_alias, b.ruleset_id, b.version) for b in s.bindings] == [
        ("auto", None, "default", 2)]
    ruleset = s.compiled[0]
    assert ruleset.routes == ["local-general", "local-code", "local-reasoning", "local-private"]
    assert ruleset.default_route == "local-general"
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
    base = load_directory(tmp_path)[0].sha256
    assert base == load_directory(RULESETS_DIR)[0].sha256
    ruleset = tmp_path / "default.yaml"
    ruleset.write_text("# a new comment\n" + ruleset.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_directory(tmp_path)[0].sha256 == base
    pattern = tmp_path / "patterns" / "code.yaml"
    pattern.write_text(pattern.read_text(encoding="utf-8").replace("  - '```'\n", ""), encoding="utf-8")
    assert load_directory(tmp_path)[0].sha256 != base


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
    assert (b.scope_model, b.scope_key_alias, b.version) == ("auto", None, 2)
    (b,) = _bindings(tmp_path, "- {model: auto, key_alias: tester, ruleset: default, version: 2}\n")
    assert b.scope_key_alias == "tester"
    for bad, match in [
        ("- {model: auto, ruleset: ghost}\n", "no ruleset 'ghost'"),
        ("- {model: auto, ruleset: default, version: 1}\n", "not baked in"),
        ("- {model: auto, ruleset: default, version: 9}\n", "not baked in"),
        ("- {model: auto, ruleset: default}\n- {model: auto, ruleset: default}\n", "bound twice"),
        ("- {model: auto, ruleset: default, extra: 1}\n", "expected"),
        ("{model: auto}\n", "a list"),
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

    store.set_bindings([Binding("auto", None, "default", 2), Binding("auto", "tester", "default", 2)])
    assert store.binding_for("auto", None).scope_key_alias is None
    assert store.binding_for("auto", "tester").scope_key_alias == "tester"
    assert store.binding_for("auto", "someone").scope_key_alias is None
    assert store.binding_for("local-code", None) is None
    assert asyncio.run(store.resolve("auto", None)).id == "default"
    assert asyncio.run(store.resolve("gpt-4", None)) is None
    assert asyncio.run(store.get("default")).version == 2
    assert asyncio.run(store.get("default", 1)) is None   # not baked in, and no database to ask
