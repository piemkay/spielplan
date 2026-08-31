"""§12's M4 exit criterion, measured by hand against a running stack.

    | **M4** | Tonight: lobby + open-rooms discovery, push join, the ~10-vote round, guest
    |        | hand-off, group combine + conflict surfacing, blind reveal, **solo mode**
    |        | (+ TV route) |
    |        | **a real Friday night resolved by the app** |

"A real Friday night" is a claim about two people on a sofa, and this script cannot make it —
the same honest limit M2's row has ("50–100 verdicts each produce visibly personal rankings" is
a statement about a household) and M3's ("stable tier lists both users **endorse**"). What is
measurable, and what this measures, is that **an evening actually resolves**: two people open a
room, answer their own rounds on their own sessions, and the app ends with one title, an
approval share, and the instruments §13 and §14 risk 6 need.

  1. **The evening resolves.** A room opens, the second member joins by the room code alone, the
     round runs, the ballot closes and a winner exists — with the wall-clock time it took, and
     the per-answer latency §6's preamble budgets at "<1.5 s per battle".
  2. **The round is per participant.** 54c's substance: the two people stop at their own pair
     counts, for their own reasons, and `ended_by` records which.
  3. **The blind property holds at the seam.** The result is refused with `still_voting` while
     one ballot is outstanding, and readable the moment the last one lands.
  4. **§13's headline number exists**: approval share, out of `session_outcome`, which is the
     only place it exists.
  5. **§14 risk 6's instruments read**: the shortlist-agreement figure over the held-out stream
     *with its n*, and the rate at which each of the three endings fired — so a reading is not
     mistaken for a measurement too small to support it.
  6. **Solo is the fast path.** Three picks and a wildcard with no round and no ballot, timed
     against the group path.

Run against a stack that is already up and has a bundle imported:

    python ops/m4_exit_criterion.py                    # defaults to http://localhost:8080
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8080")
ADMIN = {"name": "e2e-admin", "password": "e2e-first-boot-pw"}
PASSWORD = "m4-exit-criterion-password"
VERDICTS_EACH = 24

# §5.3 writes `user_score` on the fold-in tick (every 60 s), not on the verdict. An evening
# opened before it lands meets §6.2's empty pool, which is a fixture problem rather than a
# finding.
POOL_WAIT_SECONDS = 180


def member(admin: httpx.Client, name: str) -> httpx.Client:
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
    """Give this member a Ledger, through §6.1's own routes.

    The two patterns are deliberately opposed so the two people are not the same person twice —
    a household that agrees about everything cannot exercise §6.2 step 5's split.
    """
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
    client.delete("/api/rate/session")
    return done


def wait_for_pool(client: httpx.Client) -> bool:
    deadline = time.time() + POOL_WAIT_SECONDS
    while time.time() < deadline:
        solo = client.post(
            "/api/tonight/solo",
            json={"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True},
        )
        if solo.status_code == 200 and solo.json().get("picks"):
            return True
        time.sleep(3)
    return False


def play(client: httpx.Client, seat: int, answer: str = "A") -> dict:
    """One participant's whole round, and what it cost them."""
    answered, latencies = 0, []
    for _ in range(40):
        state = client.get(f"/api/tonight/seats/{seat}/round").json()
        if state.get("ended_by") or not state.get("card_token"):
            break
        started = time.perf_counter()
        written = client.post(
            f"/api/tonight/seats/{seat}/answer",
            json={"card_token": state["card_token"], "answer": answer, "latency_ms": 900},
        )
        latencies.append((time.perf_counter() - started) * 1000)
        if written.status_code != 200:
            break
        answered += 1
    final = client.get(f"/api/tonight/seats/{seat}/round").json()
    return {
        "answered": answered,
        "ended_by": final.get("ended_by") or final.get("stop_reason"),
        "slowest_ms": max(latencies) if latencies else 0.0,
        "median_ms": sorted(latencies)[len(latencies) // 2] if latencies else 0.0,
    }


def main() -> int:
    admin = httpx.Client(base_url=BASE, timeout=60)
    admin.post("/api/auth/login", json=ADMIN).raise_for_status()
    if not admin.get("/api/config").json().get("has_bundle"):
        print("no bundle imported - run `node e2e/run.mjs` first")
        return 1

    stamp = int(time.time())
    a = member(admin, f"m4-a-{stamp}")
    b = member(admin, f"m4-b-{stamp}")
    print(f"  seeded    {rate(a, [2, 2, 1, 0])} and {rate(b, [0, 1, 2, 2])} verdicts")
    for who, client in (("a", a), ("b", b)):
        if not wait_for_pool(client):
            print(f"  ABORT     member {who} has no pool after {POOL_WAIT_SECONDS}s")
            return 1

    # 1. The evening.
    evening_started = time.perf_counter()
    opened = a.post(
        "/api/tonight/sessions",
        json={"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True},
    )
    opened.raise_for_status()
    room = opened.json()
    session_id, code = room["session_id"], room["room_code"]

    # The room code alone, which is the channel §6 makes the guaranteed one.
    joined = b.post("/api/tonight/sessions/join", json={"room_code": code})
    joined.raise_for_status()
    seats = {
        "a": room["lobby"]["seats"][0]["participant_id"],
        "b": joined.json()["participant_id"],
    }
    a.post(f"/api/tonight/sessions/{session_id}/start").raise_for_status()

    rounds = {"a": play(a, seats["a"], "A"), "b": play(b, seats["b"], "B")}

    # 3. The blind property, at the seam.
    card = a.get(f"/api/tonight/sessions/{session_id}/ballot").json()
    slate = [c["title_id"] for c in card["slate"]]
    a.post(f"/api/tonight/seats/{seats['a']}/ballot", json={"approved": slate[:1]})
    early = a.get(f"/api/tonight/sessions/{session_id}/result")
    blind_held = early.status_code == 409 and early.json()["detail"]["reason"] == "still_voting"
    b.post(f"/api/tonight/seats/{seats['b']}/ballot", json={"approved": slate[:2]})

    revealed = a.get(f"/api/tonight/sessions/{session_id}/result")
    revealed.raise_for_status()
    result = revealed.json()
    elapsed = time.perf_counter() - evening_started

    # 5. §13's and §14 risk 6's instruments.
    report = a.get(f"/api/tonight/sessions/{session_id}/evaluation").json()

    # 6. Solo, timed.
    solo_started = time.perf_counter()
    solo = a.post(
        "/api/tonight/solo",
        json={"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True},
    ).json()
    solo_ms = (time.perf_counter() - solo_started) * 1000

    print()
    print(f"  1. RESOLVED     room {code} -> winner {result['winner']['name']!r}")
    print(f"                  {len(card['slate'])} on the ballot, {elapsed:.1f}s end to end")
    print(
        f"                  slowest answer {max(r['slowest_ms'] for r in rounds.values()):.0f} ms "
        f"(§6 budgets 1500)"
    )
    print(
        f"  2. PER PERSON   a {rounds['a']['answered']} pairs ({rounds['a']['ended_by']}) · "
        f"b {rounds['b']['answered']} pairs ({rounds['b']['ended_by']})"
    )
    print(f"  3. BLIND        result refused while one ballot outstanding: {blind_held}")
    print(
        f"  4. §13's FIGURE approval share {result['approval_share']:.2f} over "
        f"{result['participants']} participants (unanimous: {result['unanimous']})"
    )
    agreement = report["shortlist_agreement"]
    print(
        f"  5. §14 RISK 6   held-out pairs {agreement['pairs']}, decisive {agreement['decisive']}, "
        f"agreement {agreement['rate']}"
    )
    print(f"                  ended_by {report['ended_by']}")
    print(
        f"  6. SOLO         {len(solo['picks'])} picks + "
        f"{'a wildcard' if solo['wildcard'] else 'no wildcard'} in {solo_ms:.0f} ms, "
        f"no round, no ballot"
    )
    print(f"                  provenance: {solo['provenance']!r}")

    ok = (
        result["winner"] is not None
        and blind_held
        and 0.0 <= result["approval_share"] <= 1.0
        and len(solo["picks"]) == 3
        and all(r["ended_by"] for r in rounds.values())
    )
    print()
    print("  RESULT          " + ("the evening resolved" if ok else "SOMETHING DID NOT RESOLVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
