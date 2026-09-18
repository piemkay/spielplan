"""The `/events` namespace, mounted with no routes. Spec v2.1 §7.2, §7.3, §11; decision 332.

Empty on purpose and not empty by accident. Decision 332 settles two things about `/events` at
M5.1 and gives the second one to M5.2: that the namespace is declined by the SPA fallback the
way `/api` is (`app.py`, and it is one rule over the head segment rather than a clause each),
and that §7.2's `POST /events/jellyfin` is token-authed with the token held as a `connector_config`
secret under `jellyfin`, generated at first save and shown once on §6.6's Jellyfin card. The
first is the spine's - a namespace the shell answers is a namespace no later milestone can
mount a route into without discovering it - and the second is M5.2's, which owns the webhook,
its token, its 401 and its debounce.

So this module exists to make the namespace REAL to everything that walks the app. The router
mount guard (`test_static_contracts.py::test_every_router_under_api_is_mounted_on_the_application`)
reads every module under `api/` that exports a `router` and asserts the application serves its
paths, so M5.2's first route is mounted by adding a route here rather than by also remembering to
add an `include_router` to `app.py` - which is the edit a milestone building a webhook is most
likely to forget, and the one the fallback would then answer with the app shell.

NO SQL, EVER. `api/` decides only HTTP shapes and the rules live in the domain packages
(CLAUDE.md Conventions), so the webhook's debounce, its token check and its enqueue all belong
under `spielplan/acquire/` and `spielplan/connectors/`. `test_layering_guards.py`'s residue
ratchet records this module's count by leaving it out of `ALLOWED_RESIDUE` entirely: a module
absent from that dict holds zero by assertion, and the grown half of the ratchet fails on the
first statement that appears here rather than tolerating it under a missing key.

NO `ANONYMOUS` ENTRY EITHER. `test_route_inventory.py`'s allow-list says what a stranger is
served on purpose, and it is checked against the running app in both directions - an entry
naming a route the app does not serve is a stale exemption. `POST /events/jellyfin` will need
one, because a token in a header is not a session cookie, but the entry arrives with the route,
its reason and the test that names it. [decision 332]
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/events", tags=["events"])
