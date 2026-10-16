"""The overflow hook: which deployment of a chain alias may take a request.

    filter.py       OverflowFilter -- context fit, then the busy/down rule
    context_fit.py  ContextFit -- /tokenize counts and the too-long drop
    poller.py       Poller -- the /metrics probe task and per-backend state
    state.py        BackendState + the OK / BUSY / DOWN / UNKNOWN verdicts
    vllm.py         vLLM's server-root URLs, metric parsing, httpx clients

Wired together by hooks.handler.Handler; nothing here reads the environment.
"""
