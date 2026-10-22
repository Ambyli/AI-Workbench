"""Reading LiteLLM deployment dicts ({model_name, litellm_params, model_info}),
as the Router hands them to a deployment filter. Pure; never raises."""

from __future__ import annotations

from typing import Any, Dict, Optional

PROVIDER_PREFIXES = ("openai/", "anthropic/", "hosted_vllm/")


def params(deployment: Any) -> Dict[str, Any]:
    """The deployment's litellm_params, or {}."""
    value = deployment.get("litellm_params") if isinstance(deployment, dict) else None
    return value if isinstance(value, dict) else {}


def api_base(deployment: Any) -> Optional[str]:
    """litellm_params.api_base when set -- a backend this proxy can poll. The
    Claude deployments have none."""
    if not isinstance(deployment, dict):
        return None
    p = deployment.get("litellm_params") or {}
    base = p.get("api_base") if isinstance(p, dict) else None
    return base if isinstance(base, str) and base.strip() else None


def order(deployment: Any) -> Optional[int]:
    # Same lookup as litellm.utils._get_deployment_order.
    if not isinstance(deployment, dict):
        return None
    value = (deployment.get("litellm_params") or {}).get("order")
    if value is None:
        value = (deployment.get("model_info") or {}).get("order")
    return value


def label(deployment: Dict[str, Any]) -> str:
    """'openai/qwen3.8-solo@order1' -- how log lines name a deployment."""
    p = deployment.get("litellm_params") or {}
    return f"{p.get('model', '?')}@order{order(deployment)}"


def is_chain(deployments: Any) -> bool:
    """>= 2 deployments with >= 2 distinct `order` values. Anything else is a
    plain alias, and the hooks never touch it."""
    if not isinstance(deployments, list) or len(deployments) < 2:
        return False
    orders = {order(d) for d in deployments}
    orders.discard(None)
    return len(orders) >= 2


def positive_int(value: Any) -> Optional[int]:
    # bool is an int subclass; True is not a token budget.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def max_input_tokens(deployment: Any) -> Optional[int]:
    info = deployment.get("model_info") if isinstance(deployment, dict) else None
    return positive_int(info.get("max_input_tokens")) if isinstance(info, dict) else None


def strip_provider(model: Optional[str]) -> Optional[str]:
    """openai/qwen3.8-solo -> qwen3.8-solo; anthropic/claude-sonnet-5 -> claude-sonnet-5"""
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    for prefix in PROVIDER_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model
