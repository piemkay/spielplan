"""§12's M3 exit criterion, measured by hand against a running stack.

    | **M3** | Rank view: tiers, filters, drag-drop, comparison queue |
    |        | **stable tier lists both users endorse** |

"Endorse" is a claim about two real people and this script cannot make it — the same honest
limit M2's row has ("50-100 verdicts each produce visibly personal rankings" is a statement
about a household, not about code). What *is* measurable, and what this script measures:

  1. **Both users get a tier list at all**, and the two lists are *different* — a board that is
     the same for everybody is the crowd chart with letters on it, which is what §5.1's
     β = 0.8 blend and the per-user Ledger exist to avoid. Reported as Spearman ρ between the
     two people's orderings over the titles they have both rated.
  2. **Stable**: refitting with no new observations leaves every title in the tier it was in.
     A board that moves when nothing happened is not a board anybody can endorse.
  3. **Stable under sharpening**: answering comparison-queue pairs moves the board *less* as
     the comparisons accrue. Reported as the number of titles that change tier per ten
     comparisons, early versus late.
  4. **§13's instrument reads**: the held-out agreement figure, with its n, so the reading is
     not mistaken for a measurement it is too small to support.

Run against a stack that is already up and has a bundle imported:

    python ops/m3_exit_criterion.py                    # defaults to http://localhost:8080
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8080")
ADMIN = {"name": "e2e-admin", "password": "e2e-first-boot-pw"}
PASSWORD = "m3-exit-criterion-password"
VERDICTS_EACH = 30


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(a: list[float], b: list[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (
        sum((x - ma) ** 2 for x in ra) ** 0.5 * sum((y - mb) ** 2 for y in rb) ** 0.5
    )
    return num / den if den else float("nan")


def make_member(admin: httpx.Client, name: str) -> httpx.Client:
    created = admin.post("/api/setup/members", json={"name": name, "role": "member"})
    created.raise_for_status()
    otp = created.json()["one_time_password"]
    client = httpx.Client(base_url=BASE, timeout=60)
    client.post("/api/auth/login", json={"name": name, "password": otp}).raise_for_status()
    client.post(
        "/api/auth/password", json={"current_password": otp, "new_password": PASSWORD}
    ).raise_for_status()
    return client


def rate(client: httpx.Client, pattern: list[int], target: int = VERDICTS_EACH) -> int:
    """Rate `target` titles, cycling `pattern` so the two people disagree by construction."""
    client.post("/api/rate/session", json={"restart": True, "kinds": ["movie"]})
    done = 0
    for _ in range(target * 4):
        card = client.get("/api/rate").json().get("card")
        if not card:
            break
        if card["type"] == "sweep":
            client.post(
                "/api/rate/verdict",
                json={"card_token": card["token"], "value": pattern[done % len(pattern)]},
            )
            done += 1
        else:
            client.post("/api/rate/duel", json={"card_token": card["token"], "outcome": "A"})
        if done >= target:
            break
    return done


def board(client: httpx.Client) -> dict[int, int]:
    payload = client.get("/api/rank", params={"kind": "movie"}).json()
    return {e["title_id"]: t["index"] for t in payload["tiers"] for e in t["entries"]}


def order(client: httpx.Client) -> list[int]:
    """Titles best-first, which is the ordering the board renders."""
    payload = client.get("/api/rank", params={"kind": "movie"}).json()
    return [e["title_id"] for t in payload["tiers"] for e in t["entries"]]


def sharpen(client: httpx.Client, n: int) -> int:
    answered = 0
    for _ in range(n * 3):
        served = client.get("/api/rank/queue", params={"kind": "movie"}).json()
        if not served.get("pair"):
            break
        written = client.post(
            "/api/rank/queue/answer",
            json={"pair": served["pair"]["token"], "outcome": "A"},
        )
        if written.status_code == 200:
            answered += 1
        if answered >= n:
            break
    return answered


def moved(before: dict[int, int], after: dict[int, int]) -> int:
    return sum(1 for t, tier in after.items() if before.get(t) != tier)


def main() -> int:
    admin = httpx.Client(base_url=BASE, timeout=60)
    admin.post("/api/auth/login", json=ADMIN).raise_for_status()
    if not admin.get("/api/config").json().get("has_bundle"):
        print("no bundle imported — run `node e2e/run.mjs` first")
        return 1

    stamp = int(time.time())
    print("== §12 M3 exit criterion: stable tier lists both users endorse ==\n")

    people = {}
    for name, pattern in (("liked-first", [2, 2, 1, 0]), ("disliked-first", [0, 1, 2, 2])):
        client = make_member(admin, f"m3-{name}-{stamp}")
        rated = rate(client, pattern)
        people[name] = client
        print(f"  {name:14} rated {rated} titles")

    # The refit the board reads is the nightly one; the incremental path has already run per
    # observation, so this is the exact-vs-immediate reconciliation §5.2 names.
    print("\n  waiting for the nightly refit's incremental equivalent to settle…")
    time.sleep(2)

    boards = {name: board(c) for name, c in people.items()}
    orders = {name: order(c) for name, c in people.items()}
    for name, b in boards.items():
        spread = sorted({tier for tier in b.values()})
        print(f"  {name:14} {len(b)} titles on the board, tiers occupied: {spread}")

    shared = [t for t in orders["liked-first"] if t in boards["disliked-first"]]
    if len(shared) >= 2:
        a = [orders["liked-first"].index(t) for t in shared]
        b = [orders["disliked-first"].index(t) for t in shared]
        rho = spearman([float(x) for x in a], [float(x) for x in b])
        print(f"\n  1. PERSONAL: Spearman rho between the two orderings = {rho:+.3f} "
              f"over {len(shared)} shared titles")
        print("     (1.0 would mean one board for the household; §5.1's whole point is that "
              "it is not)")

    print("\n  2. STABLE UNDER RE-READ: same request twice, no observations in between")
    for name, client in people.items():
        again = board(client)
        print(f"     {name:14} titles whose tier changed: {moved(boards[name], again)}")

    print("\n  3. STABLE UNDER SHARPENING: tier changes per 10 comparisons")
    for name, client in people.items():
        early_before = board(client)
        early = sharpen(client, 10)
        early_after = board(client)
        late = sharpen(client, 10)
        late_after = board(client)
        print(
            f"     {name:14} first {early:2} comparisons moved "
            f"{moved(early_before, early_after):2} titles; "
            f"next {late:2} moved {moved(early_after, late_after):2}"
        )

    print("\n  4. §13's INSTRUMENT (the only data admitted to evaluate the tier model)")
    for name, client in people.items():
        client.post("/api/auth/preferences", json={"show_model": True})
        model = client.get("/api/rank", params={"kind": "movie"}).json().get("model")
        held = model["held_out"] if model else {}
        rate_ = held.get("rate")
        print(
            f"     {name:14} held-out pairs {held.get('pairs', 0)}, "
            f"decisive {held.get('decisive', 0)}, ties {held.get('ties', 0)}, "
            f"undecided {held.get('undecided', 0)}, "
            f"agreement {'n/a' if rate_ is None else f'{rate_:.2f}'}"
        )

    print(
        "\n  NOT MEASURED HERE: 'endorse'. That is two people looking at their own board and\n"
        "  saying it is right, and no script can stand in for it — the same limit §12's M2 row\n"
        "  has. What is above is the machinery being stable and personal; the endorsement is\n"
        "  the household's to give."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
