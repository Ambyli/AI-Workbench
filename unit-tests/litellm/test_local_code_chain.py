"""local-code: the chain's coding settings in ai/litellm/litellm_config.yaml.

`local-code` has the same ladder as `local-general`; what differs is what each
deployment sends. (How a request GETS there is the route-policy engine's
business -- ai/route-policy -- and is tested there.)

* Config invariants (need PyYAML): every local-code deployment carries the
  same coding system prompt (one YAML anchor), qwen gets reasoning_effort
  high and muse reasoning_strength xhigh through chat_template_kwargs, the
  prompt never says "reasoning strength" (muse's template would let it win
  over the kwarg), 32768-token answers fit each backend's max-model-len,
  local-general & co. are untouched, and `auto` -- an alias, not a
  deployment -- adds no max_tokens of its own.
* Against real LiteLLM (skipped unless importable): a Router built from the
  config really sends local-code's prompt, kwargs and max_tokens.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"

CONFIG = _LITELLM_DIR / "litellm_config.yaml"
VLLM_COMPOSE = _ROOT / "ai" / "vllm" / "docker-compose.vllm.yml"

try:
    import yaml
except ImportError:
    yaml = None

needs_yaml = pytest.mark.skipif(yaml is None, reason="needs PyYAML")

CODE_PROMPT_LINES = (
    "You are working on code with tools.",
    "Your turn ends as soon as you reply without a tool call.",
    "edit_file",
    "write_files only for new files or complete rewrites",
    "read_file (offset/limit)",
    "When a tool returns an error, diagnose it and retry in the same turn.",
    "Run the code or its tests to verify before saying it works.",
    "Reply without a tool call only when the task is done",
)


def _cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _group(name: str) -> dict:
    """{litellm_params.model: deployment} of one model group."""
    out = {}
    for m in _cfg()["model_list"]:
        if m["model_name"] == name:
            out[m["litellm_params"]["model"]] = m
    assert out, f"no deployments for {name}"
    return out


def _max_model_len(service: str) -> int:
    """--max-model-len of a vLLM service, read from its compose block."""
    text = VLLM_COMPOSE.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  \S)", text)
    assert m, f"no service {service} in {VLLM_COMPOSE.name}"
    found = re.findall(r"--max-model-len\s+(\d+)", m.group(1))
    assert len(found) == 1, f"{service}: expected one --max-model-len, got {found}"
    return int(found[0])


@needs_yaml
def test_the_prompt_is_one_anchor_in_the_file():
    text = CONFIG.read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^\s+litellm_system_prompt: &code_system_prompt\b", text)) == 1
    assert len(re.findall(r"(?m)^\s+litellm_system_prompt: \*code_system_prompt\s*$", text)) == 2


@needs_yaml
def test_every_local_code_deployment_has_the_same_coding_prompt():
    group = _group("local-code")
    assert set(group) == {"openai/qwen3.8-solo", "openai/muse-glimmer", "anthropic/claude-sonnet-5"}
    prompts = {m["litellm_params"].get("litellm_system_prompt") for m in group.values()}
    assert len(prompts) == 1
    prompt = prompts.pop()
    assert isinstance(prompt, str) and prompt == prompt.strip()
    for line in CODE_PROMPT_LINES:
        assert line in prompt, line
    # muse-glimmer's template takes a "Reasoning strength:" line in the system
    # prompt over the reasoning_strength kwarg -- so the prompt must not have one.
    assert "reasoning strength" not in prompt.lower()
    assert "reasoning_strength" not in prompt.lower() and "reasoning effort" not in prompt.lower()


@needs_yaml
def test_local_code_reasoning_goes_through_each_models_own_template_kwarg():
    group = _group("local-code")
    qwen = group["openai/qwen3.8-solo"]["litellm_params"]
    muse = group["openai/muse-glimmer"]["litellm_params"]
    claude = group["anthropic/claude-sonnet-5"]["litellm_params"]
    # qwen_fixed_chat_template.jinja maps high -> xhigh; muse's stock template defaults to high.
    assert qwen["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}
    assert muse["extra_body"]["chat_template_kwargs"] == {"reasoning_strength": "xhigh"}
    assert "extra_body" not in claude and "max_tokens" not in claude


@needs_yaml
def test_local_code_sampling_is_local_generals():
    general, code = _group("local-general"), _group("local-code")
    for model in ("openai/qwen3.8-solo", "openai/muse-glimmer"):
        g, c = general[model]["litellm_params"], code[model]["litellm_params"]
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "api_base",
                    "order", "stream_timeout", "timeout"):
            assert c[key] == g[key], (model, key)
        g_extra = {k: v for k, v in g["extra_body"].items() if k != "chat_template_kwargs"}
        c_extra = {k: v for k, v in c["extra_body"].items() if k != "chat_template_kwargs"}
        assert c_extra == g_extra, model  # top_k / min_p kept next to the new kwarg


@needs_yaml
def test_local_code_answers_are_32768_tokens_and_fit_the_backend():
    group = _group("local-code")
    for model in ("openai/qwen3.8-solo", "openai/muse-glimmer"):
        m = group[model]
        assert m["litellm_params"]["max_tokens"] == 32768
        assert m["model_info"]["max_output_tokens"] == 32768
    # The context fit drops a deployment when count + max_tokens > max_model_len,
    # so max_input_tokens says the same thing: qwen 131072 - 32768 = 98304.
    assert group["openai/qwen3.8-solo"]["model_info"]["max_input_tokens"] == 98304


@needs_yaml
@pytest.mark.parametrize("chain", ["local-general", "local-code", "local-reasoning", "local-private"])
def test_every_local_chain_deployment_fits_input_plus_output_in_max_model_len(chain):
    for model, m in _group(chain).items():
        if not model.startswith("openai/"):
            continue
        service = model.split("/", 1)[1]
        assert m["model_info"]["max_input_tokens"] + m["litellm_params"]["max_tokens"] <= _max_model_len(service), (
            chain, model)


@needs_yaml
@pytest.mark.parametrize("chain", ["local-general", "local-reasoning", "local-private"])
def test_other_chains_are_unchanged(chain):
    for model, m in _group(chain).items():
        p = m["litellm_params"]
        assert "litellm_system_prompt" not in p, (chain, model)
        assert "chat_template_kwargs" not in (p.get("extra_body") or {}), (chain, model)
        if model.startswith("openai/"):
            assert p["max_tokens"] == 16384, (chain, model)


@needs_yaml
def test_auto_is_an_alias_and_adds_no_max_tokens():
    """`auto` is router_settings.model_group_alias -> local-private, not a
    deployment: it has no litellm_params, so it can send no max_tokens of its
    own and each chain's deployment decides (a client-sent value still wins)."""
    cfg = _cfg()
    assert not [m for m in cfg["model_list"] if m["model_name"] == "auto"]
    assert cfg["router_settings"]["model_group_alias"] == {"auto": "local-private"}


# ── against real LiteLLM ──────────────────────────────────────────────────────
try:
    import litellm  # noqa: E402
    from litellm.integrations.custom_logger import CustomLogger  # noqa: E402
except ImportError:
    litellm = None

needs_litellm = pytest.mark.skipif(litellm is None, reason="needs litellm")


@needs_litellm
@needs_yaml
def test_router_sends_local_codes_prompt_kwargs_and_max_tokens():
    """A Router built from the real config: what leaves LiteLLM for each
    local-code order, captured before the HTTP call (order 1 and 2 point at
    a closed port; the capture is all that matters)."""
    captured = []

    class Capture(CustomLogger):
        def log_pre_api_call(self, model, messages, kwargs):
            captured.append((kwargs.get("additional_args") or {}).get("complete_input_dict") or {})

    model_list = []
    for m in _cfg()["model_list"]:
        if m["model_name"] in ("local-code", "local-general") and m["litellm_params"]["model"].startswith("openai/"):
            m = copy.deepcopy(m)
            m["litellm_params"]["api_base"] = "http://127.0.0.1:9/v1"
            model_list.append(m)
    router = litellm.Router(model_list=model_list, num_retries=0)
    old = litellm.callbacks
    litellm.callbacks = [Capture()]
    try:
        msgs = [{"role": "system", "content": "owui system"}, {"role": "user", "content": "hi"}]
        for group in ("local-code", "local-general"):
            try:
                asyncio.run(router.acompletion(model=group, messages=copy.deepcopy(msgs), timeout=2))
            except Exception:
                pass
    finally:
        litellm.callbacks = old
    bodies = [b for b in captured if b.get("messages")]
    code_qwen = next(b for b in bodies if b["messages"][0]["content"].startswith("You are working on code")
                     and b.get("model") == "qwen3.8-solo")
    assert code_qwen["messages"][0]["content"].endswith("\n\nowui system")
    assert code_qwen["max_tokens"] == 32768
    assert code_qwen["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}
    assert code_qwen["extra_body"]["top_k"] == 20
    general_qwen = [b for b in bodies if b.get("model") == "qwen3.8-solo"
                    and b["messages"][0]["content"] == "owui system"]
    assert general_qwen and general_qwen[0]["max_tokens"] == 16384
    assert "chat_template_kwargs" not in general_qwen[0].get("extra_body", {})
