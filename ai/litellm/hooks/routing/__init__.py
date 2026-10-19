"""Routing `auto` to a chain alias before LiteLLM picks a deployment.

    policy_client.py  PolicyClient -- awaits the route-policy service's /decide
                      on every `auto` request and rewrites it to the chosen
                      chain (else local-private); fires /observe after it
    invariants.py     the hook's own copy of the two route invariants
    stash.py          the per-request record the post-call hooks read
"""
