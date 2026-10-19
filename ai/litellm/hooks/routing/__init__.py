"""Request routing edits made before LiteLLM picks a model group.

    code_route.py     CodeRoute -- local-general -> local-code for coding chats
    policy_client.py  PolicyClient -- route-policy shadow mode: /decide on every
                      `auto` request, /observe after it; fired, never awaited
"""
