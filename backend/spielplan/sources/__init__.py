"""§8 stage 2's source adapters and the registry they declare themselves in.

Adapters fetch, never parse (decision 372); every request goes through `acquire/fetch.Fetcher`.
Letterboxd is absent by decision 374.
"""
