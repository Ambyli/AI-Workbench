"""This deployment's LiteLLM proxy hooks -- one CustomLogger, three jobs.

LiteLLM loads them through the shim ai/litellm/overflow.py
(``litellm_settings.callbacks: ["overflow.handler"]``), which imports this
package NORMALLY -- so it is in sys.modules, relative imports work and
@dataclass is safe here -- and builds ``handler = Handler()``. Mounted
read-only at /app/hooks beside /app/overflow.py by
ai/litellm/docker-compose.litellm.yml; the one stdlib-only part of
shared/common it needs (common.conversation) is mounted at /app/common the
same way.

    handler.py      Handler(CustomLogger): every hook LiteLLM dispatches on,
                    each delegating to one of the jobs below
    settings.py     every environment variable, read once at import
    log.py          the `zeo.overflow` logger and the line throttle
    deployments.py  reading LiteLLM deployment dicts
    overflow/       job 1: spill a chain alias to its next `order` when a
                    local vLLM is busy, down, or too small for the prompt
    footer/         job 2: the `auto` footer naming the backend that answered
    routing/        job 3: the route-policy client -- every `auto` request
                    awaits /decide and is rewritten to the chosen chain alias
                    (local-private when the engine cannot or may not decide),
                    then /observe reports what answered

Importing this package, or a helper module in it, builds no handler and
starts nothing; only hooks.settings reads the environment (once, when first
imported). See ai/litellm/LITELLM.md.
"""
