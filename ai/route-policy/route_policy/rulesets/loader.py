"""Read ruleset files, inline their refs, and load the seed bindings.

The image bakes ``ai/route-policy/rulesets/`` in (Dockerfile); startup seeds
it into route-policy-db (``rulesets.store.seed_and_load``):

    rulesets/<anything>.yaml     one ruleset document each (top level only)
    rulesets/bindings.yaml       the seed bindings -- not a ruleset
    rulesets/patterns/*.yaml     keyword rules, referenced by `patterns_ref`
    rulesets/examples/*.yaml     example sets, referenced by `examples_ref`

**Refs.** An evaluator's ``config.patterns_ref: patterns/code.yaml`` is
inlined as ``config.rule``, and ``config.examples_ref: examples/routes.yaml``
as ``config.examples`` (the ref is kept, as provenance). A config that
already has the target key is left alone -- a stored document, as GET /rulesets
returns it, can be posted back to POST /evaluate as is. A ref is a relative
path inside the rulesets directory -- an absolute path or one that climbs
out with ``..`` is refused, also for documents sent to POST /evaluate.

**Content hash.** A loaded ruleset's identity is the SHA-256 of its resolved
document (``model.content_sha256``): YAML comments do not count, anything
else does -- the description, a pattern in a referenced file. A stored
id + version whose hash differs fails startup (``rulesets.store``), so a
changed ruleset must get a new version.

**Bindings** (``bindings.yaml``)::

    - model: auto               # the requested model
      key_alias: null           # optional: only requests from this LiteLLM key alias
      ruleset: default
      version: null             # null / omitted = the highest version baked in
      shadow: [default-llm, default-signals]   # optional: rulesets decided beside it, logged, never applied;
                                # each an id (highest version) or {ruleset: id, version: n}

Two seed rules, checked here so they stop the image build as well as
startup: the PRIMARY ruleset of a binding must be ``mode: enforce`` (a shadow
primary would answer ``apply: false`` and send every turn to local-private),
and every SHADOW slot must be ``mode: shadow``.

Startup owns the bindings it seeds (it re-points them, and replaces their
shadow slots, on every start); the write API that edits them is phase 6. A
stored version that is no longer baked in (``default@1`` after phase 3)
stays in the database and is compiled from there on first use.
"""

from __future__ import annotations

import copy
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .model import CompiledRuleset, content_sha256
from .validate import compile_ruleset

BINDINGS_FILE = "bindings.yaml"
# ref key -> the config key it is inlined as.
REF_KEYS = {"patterns_ref": "rule", "examples_ref": "examples"}


class RulesetFileError(ValueError):
    """A ruleset file (or a file it references) cannot be read or parsed."""


@dataclass(frozen=True)
class LoadedRuleset:
    document: Dict[str, Any]     # resolved: refs inlined
    sha256: str
    source: str                  # where it came from, e.g. "rulesets/default.yaml"

    @property
    def key(self) -> tuple:
        return (self.document.get("id"), self.document.get("version"))


@dataclass(frozen=True)
class Binding:
    scope_model: str
    scope_key_alias: Optional[str]
    ruleset_id: str
    version: int
    # (ruleset id, version) of each shadow slot, in order: decided after the
    # primary has answered, logged with shadow_of, never applied.
    shadows: Tuple[Tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Seed:
    rulesets: List[LoadedRuleset]
    compiled: List[CompiledRuleset]
    bindings: List[Binding]


def _read_yaml(path: pathlib.Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesetFileError(f"{path.name}: cannot be read: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesetFileError(f"{path.name}: not valid YAML: {exc}") from exc


def ref_path(base_dir: pathlib.Path, ref: Any) -> pathlib.Path:
    """The file a ref names, refusing anything outside ``base_dir``."""
    if not isinstance(ref, str) or not ref.strip():
        raise RulesetFileError(f"ref {ref!r} is not a relative path")
    pure = pathlib.PurePosixPath(ref.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or ref.startswith(("/", "\\")) or ":" in ref:
        raise RulesetFileError(f"ref {ref!r} must be a relative path inside the rulesets directory")
    base = base_dir.resolve()
    path = (base / pure).resolve()
    if base not in path.parents:
        raise RulesetFileError(f"ref {ref!r} must be a relative path inside the rulesets directory")
    return path


def resolve_refs(document: Any, base_dir: pathlib.Path) -> Any:
    """A deep copy of ``document`` with every evaluator's ``*_ref`` inlined.

    Leaves anything it does not recognise alone -- validation reports it."""
    if not isinstance(document, dict):
        return document
    doc = copy.deepcopy(document)
    evaluators = doc.get("evaluators")
    if not isinstance(evaluators, list):
        return doc
    for i, entry in enumerate(evaluators):
        config = entry.get("config") if isinstance(entry, dict) else None
        if not isinstance(config, dict):
            continue
        for ref_key, target in REF_KEYS.items():
            # Already resolved (a stored document, as GET /rulesets/{id}
            # returns it): the ref is provenance only and is not re-read.
            if ref_key not in config or target in config:
                continue
            path = ref_path(base_dir, config[ref_key])
            if not path.is_file():
                raise RulesetFileError(f"evaluators[{i}].config.{ref_key}: {config[ref_key]!r} does not exist")
            config[target] = _read_yaml(path)
    return doc


def load_file(path: pathlib.Path, base_dir: pathlib.Path) -> LoadedRuleset:
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise RulesetFileError(f"{path.name}: a ruleset file holds one mapping")
    try:
        doc = resolve_refs(raw, base_dir)
    except RulesetFileError as exc:
        raise RulesetFileError(f"{path.name}: {exc}") from exc
    return LoadedRuleset(doc, content_sha256(doc), f"rulesets/{path.name}")


def ruleset_files(base_dir: pathlib.Path) -> List[pathlib.Path]:
    return sorted(p for p in base_dir.glob("*.yaml") if p.name != BINDINGS_FILE)


def load_directory(base_dir: pathlib.Path) -> List[LoadedRuleset]:
    """Every ruleset file in ``base_dir`` (top level, ``*.yaml`` but
    ``bindings.yaml``), refs inlined. Two files with the same id + version
    are an error."""
    out: List[LoadedRuleset] = []
    seen: Dict[tuple, str] = {}
    for path in ruleset_files(base_dir):
        loaded = load_file(path, base_dir)
        if loaded.key in seen:
            raise RulesetFileError(f"{path.name}: {loaded.key[0]}@{loaded.key[1]} is also in {seen[loaded.key]}")
        seen[loaded.key] = path.name
        out.append(loaded)
    return out


def _baked_version(where: str, versions: Dict[str, List[int]], ruleset_id: Any, version: Any) -> int:
    """``version`` of ``ruleset_id`` (None = the highest baked in), refusing
    one that is not baked in."""
    if not isinstance(ruleset_id, str) or not ruleset_id:
        raise RulesetFileError(f"{where}: ruleset is a required string")
    if ruleset_id not in versions:
        raise RulesetFileError(f"{where}: no ruleset {ruleset_id!r} is baked in")
    if version is None:
        return max(versions[ruleset_id])
    if isinstance(version, bool) or version not in versions[ruleset_id]:
        raise RulesetFileError(f"{where}: {ruleset_id}@{version} is not baked in")
    return int(version)


def load_bindings(base_dir: pathlib.Path, rulesets: List[LoadedRuleset]) -> List[Binding]:
    """``bindings.yaml``, each version resolved against the loaded rulesets
    (omitted = the highest version of that id), the primary checked to be
    ``enforce`` and every shadow slot ``shadow``."""
    path = base_dir / BINDINGS_FILE
    if not path.exists():
        return []
    raw = _read_yaml(path) or []
    if not isinstance(raw, list):
        raise RulesetFileError(f"{BINDINGS_FILE}: a list of bindings")
    versions: Dict[str, List[int]] = {}
    modes: Dict[Tuple[str, int], Any] = {}
    for loaded in rulesets:
        versions.setdefault(str(loaded.key[0]), []).append(loaded.key[1])
        modes[(str(loaded.key[0]), loaded.key[1])] = loaded.document.get("mode")
    out: List[Binding] = []
    seen = set()
    for i, item in enumerate(raw):
        where = f"{BINDINGS_FILE}[{i}]"
        if not isinstance(item, dict) or set(item) - {"model", "key_alias", "ruleset", "version", "shadow"}:
            raise RulesetFileError(f"{where}: expected {{model, key_alias?, ruleset, version?, shadow?}}")
        model, ruleset_id = item.get("model"), item.get("ruleset")
        key_alias = item.get("key_alias")
        if not isinstance(model, str) or not model or not isinstance(ruleset_id, str) or not ruleset_id:
            raise RulesetFileError(f"{where}: model and ruleset are required strings")
        if key_alias is not None and (not isinstance(key_alias, str) or not key_alias):
            raise RulesetFileError(f"{where}: key_alias is a non-empty string or null")
        version = _baked_version(where, versions, ruleset_id, item.get("version"))
        if modes[(ruleset_id, version)] != "enforce":
            raise RulesetFileError(
                f"{where}: {ruleset_id}@{version} is mode {modes[(ruleset_id, version)]!r}; a binding's "
                "primary ruleset must be `enforce` (a shadow primary sends every turn to local-private) "
                "-- bind a shadow ruleset under `shadow:` instead")
        raw_shadows = item.get("shadow")
        if raw_shadows is None:
            raw_shadows = []
        if not isinstance(raw_shadows, list):
            raise RulesetFileError(f"{where}.shadow: a list of ruleset ids or {{ruleset, version?}}")
        shadows: List[Tuple[str, int]] = []
        for j, slot in enumerate(raw_shadows):
            slot_where = f"{where}.shadow[{j}]"
            if isinstance(slot, str):
                slot = {"ruleset": slot}
            if not isinstance(slot, dict) or set(slot) - {"ruleset", "version"}:
                raise RulesetFileError(f"{slot_where}: expected a ruleset id or {{ruleset, version?}}")
            ref = (slot.get("ruleset"),
                   _baked_version(slot_where, versions, slot.get("ruleset"), slot.get("version")))
            if modes[ref] != "shadow":
                raise RulesetFileError(f"{slot_where}: {ref[0]}@{ref[1]} is mode {modes[ref]!r}; "
                                       "a shadow slot takes a `shadow` ruleset")
            if ref in shadows:
                raise RulesetFileError(f"{slot_where}: {ref[0]}@{ref[1]} is listed twice")
            shadows.append(ref)
        if (model, key_alias) in seen:
            raise RulesetFileError(f"{where}: {model} / {key_alias} is bound twice")
        seen.add((model, key_alias))
        out.append(Binding(model, key_alias, ruleset_id, version, tuple(shadows)))
    return out


def load_seed(base_dir: pathlib.Path) -> Seed:
    """Everything startup seeds, read and VALIDATED -- a file that does not
    compile stops the service here (and the image build: the Dockerfile runs
    this)."""
    rulesets = load_directory(base_dir)
    compiled = [compile_ruleset(r.document, sha256=r.sha256) for r in rulesets]
    bindings = load_bindings(base_dir, rulesets)
    return Seed(rulesets, compiled, bindings)
