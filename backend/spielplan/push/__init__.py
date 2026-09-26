"""Web-push. `keys` holds the VAPID pair, sealed like every secret; `send` hand-rolls RFC 8291/8292
over `cryptography` and `httpx`. Best-effort: nothing raises into a caller (§6).
"""
