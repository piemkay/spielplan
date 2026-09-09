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
    created = admin.post("/api/admin/users", json={"name": name, "role": "member"})
    created.raise_for_status()
    otp = created.json()["one_time_password"]
    client = httpx.Client(base_url=BASE, timeout=60)
    client.post("/api/auth/login", json={"name": name, "password": otp}).raise_for_status()
    client.post(
        "/api/auth/password", json={"current_password": otp, "new_password": PASSWORD}
    ).raise_for_status()
    return client


def rate(client: httpx.Client, pattern: list[int], target: int = VERDICTS_EACH) -> int:
    """Rate `target` titles, cycling `pattern` so the two people disagree by construction.

    Every write's status is read. Without `raise_for_status` a stack that refuses every
    verdict -- an expired session, a 500 out of the fold-in -- returned `done` incremented
    once per ignored refusal, and the whole script then measured two empty Ledgers and
    reported the boards they produce as M3's exit criterion.
    [M4.8 ti-m3-and-m4-exit-scripts-cannot-report-their-own-failures]
    """
    client.post(
        "/api/rate/session", json={"restart": True, "kinds": ["movie"]}
    ).raise_for_status()
    done = 0
    for _ in range(target * 4):
        card = client.get("/api/rate").json().get("card")
        if not card:
            break
        if card["type"] == "sweep":
            client.post(
                "/api/rate/verdict",
                json={"card_token": card["token"], "value": pattern[done % len(pattern)]},
            ).raise_for_status()
            done += 1
        else:
            client.post(
                "/api/rate/duel", json={"card_token": card["token"], "outcome": "A"}
            ).raise_for_status()
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


def sharpen(client: httpx.Client, n: int) -> tuple[int, int]:
    """(comparisons answered, requests the server refused).

    The refusals are returned rather than dropped: section 3 reads tier movement *per ten
    comparisons*, so a run in which six of ten answers were refused reports a board that
    barely moved and calls it stability. This counts only 200s as answered, exactly as
    before, and now says how many were not.
    [M4.8 ti-m3-and-m4-exit-scripts-cannot-report-their-own-failures]

    Both requests in the loop are read, not only the write. A refused *draw* is the shape that
    hid best: `app.py`'s handlers answer a database fault with `{"detail": ...}`, so the body
    parses, carries no `pair`, and looks exactly like an exhausted queue -- the loop broke on
    its first iteration and section 3 printed "first 0 comparisons moved 0 titles" with no
    refusal note at all, which is a board that moved nothing over a reading never taken. The
    draw and the answer fail independently: `GET /api/rank/queue` runs the candidate read, the
    draw and the seal, none of which `GET /api/rank` touches.
    [M4.8 review cycle 2: m48-rev2-sharpen-counts-only-half-its-refusals]
    """
    answered, refused = 0, 0
    for _ in range(n * 3):
        drawn = client.get("/api/rank/queue", params={"kind": "movie"})
        if drawn.status_code != 200:
            refused += 1
            break
        served = drawn.json()
        if not served.get("pair"):
            break
        written = client.post(
            "/api/rank/queue/answer",
            json={"pair": served["pair"]["token"], "outcome": "A"},
        )
        if written.status_code == 200:
            answered += 1
        else:
            refused += 1
        if answered >= n:
            break
    return answered, refused


def moved(before: dict[int, int], after: dict[int, int]) -> int:
    return sum(1 for t, tier in after.items() if before.get(t) != tier)


def main() -> int:
    admin = httpx.Client(base_url=BASE, timeout=60)
    admin.post("/api/auth/login", json=ADMIN).raise_for_status()
    if not admin.get("/api/config").json().get("has_bundle"):
        print("no bundle imported - run `node e2e/run.mjs` first")
        return 1

    stamp = int(time.time())
    print("== §12 M3 exit criterion: stable tier lists both users endorse ==\n")

    people = {}
    rated: dict[str, int] = {}
    for name, pattern in (("liked-first", [2, 2, 1, 0]), ("disliked-first", [0, 1, 2, 2])):
        client = make_member(admin, f"m3-{name}-{stamp}")
        try:
            rated[name] = rate(client, pattern)
        except httpx.HTTPError as exc:
            # `rate` raises on the first refused write; this is where that becomes a
            # sentence. Letting the exception escape would exit non-zero too, but with a
            # traceback instead of the name of the precondition that failed, and the
            # precondition is the whole point of the exit code.
            print(f"  PRECONDITION FAILED: seeding {name} stopped on a refused write -- "
                  f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
            return 1
        people[name] = client
        print(f"  {name:14} rated {rated[name]} titles")

    # No wait. The board reads the fit that `rate/session.py:840-847` updates *inside* the
    # verdict route, synchronously, before it answers -- so every observation this script
    # wrote is already folded in by the time `rate()` returns. The `time.sleep(2)` that
    # stood here paced nothing, and a sleep in front of a check is how an asynchronous
    # refit would come to look synchronous: long enough to hide the race, short enough to
    # fail on a slower machine. The exact-vs-immediate reconciliation §5.2 names is what
    # section 2 below measures, by re-reading rather than by waiting.
    # [M4.8 ti-m3-and-m4-exit-scripts-cannot-report-their-own-failures]
    print("\n  no wait for a refit: the verdict route folds each observation in synchronously")

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
    else:
        # Printed, not skipped. This is the headline section -- the one the §12 row's
        # "personal" claim rests on -- and it used to vanish without a word when the two
        # people happened to rate disjoint sets, leaving a report whose numbering jumps
        # from the seeding lines to section 2 and reads like a complete run.
        print(f"\n  1. PERSONAL: NOT MEASURED (only {len(shared)} shared titles; Spearman "
              "rho needs two)")

    print("\n  2. STABLE UNDER RE-READ: same request twice, no observations in between")
    reread_moved: dict[str, int] = {}
    for name, client in people.items():
        again = board(client)
        reread_moved[name] = moved(boards[name], again)
        print(f"     {name:14} titles whose tier changed: {reread_moved[name]}")

    print("\n  3. STABLE UNDER SHARPENING: tier changes per 10 comparisons")
    for name, client in people.items():
        early_before = board(client)
        early, early_refused = sharpen(client, 10)
        early_after = board(client)
        late, late_refused = sharpen(client, 10)
        late_after = board(client)
        refused = early_refused + late_refused
        print(
            f"     {name:14} first {early:2} comparisons moved "
            f"{moved(early_before, early_after):2} titles; "
            f"next {late:2} moved {moved(early_after, late_after):2}"
            + (f" ({refused} requests refused)" if refused else "")
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
        "  saying it is right, and no script can stand in for it - the same limit §12's M2 row\n"
        "  has. What is above is the machinery being stable and personal; the endorsement is\n"
        "  the household's to give."
    )

    # The exit code, over the preconditions this script CAN check. It used to be a bare
    # `return 0`: every run succeeded, including the runs where nobody was rated, both
    # boards came back empty and every number above was printed off an empty dict. These
    # three are not §12's criterion -- "endorse" is not checkable and the paragraph above
    # says so -- they are the conditions under which the readings above mean anything.
    # [M4.8 ti-m3-and-m4-exit-scripts-cannot-report-their-own-failures]
    problems: list[str] = []
    for name, count in rated.items():
        if count < VERDICTS_EACH:
            problems.append(f"{name} rated {count} of {VERDICTS_EACH} titles")
    for name, tiers in boards.items():
        if not tiers:
            problems.append(f"{name} has no board at all")
    if orders["liked-first"] == orders["disliked-first"]:
        # §5.1's whole point: one board for the household is the crowd chart with letters
        # on it, and two people constructed to disagree must not produce it.
        problems.append("both people got the same ordering")
    for name, n in reread_moved.items():
        if n:
            problems.append(f"{name}'s board moved {n} titles on a re-read with no observations")

    print()
    if problems:
        print("  PRECONDITIONS NOT MET, so nothing above is a reading:")
        for line in problems:
            print(f"    - {line}")
    else:
        print("  PRECONDITIONS MET   two boards exist, they differ, a re-read moves nothing,")
        print(f"                      and both people reached {VERDICTS_EACH} verdicts")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
