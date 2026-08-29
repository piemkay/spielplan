# Spielplan — proposals for spec v2.2

*Working document, 2026-08-29. Produced by reading the UI prototype (design project
"Media graph app mockups", file `Media Graph.dc.html`) against `spielplan-spec_v2.1.md`,
surface by surface, with an adversarial pass over every claimed divergence.*

**Status:** proposals, not spec. Nothing here is normative until it lands in a v2.2 of
`spielplan-spec_v2.1.md`. 161 numbered proposals. **All seven owner decisions were taken on
2026-08-29** and are recorded at the end; one of them (54) replaced the question with a
redesign, written up as §6.2 — Tonight, rewritten. Proposals 148–161 were added by the
completeness pass and sit in their spec section, so the numbering is not in document order.

**Method.** Six readers inventoried one surface cluster each and classified every item as
spec-covered / spec-silent / spec-conflict / spec-only. A second agent per surface tried to
*refute* each claim by re-reading both sources — 68 of 235 claims were killed there, most for
re-litigating a settled owner decision or for the spec already covering the point elsewhere.
167 survivors were synthesised into the proposals below, then a completeness critic swept the
prototype for what the readers missed.

**Two owner decisions post-date the prototype and are not re-opened here:** there is no
`forgotten` / "don't remember" state (a title you cannot recall is plain `unseen`), and the
Tonight flow uses ~10 candidate votes instead of a visible shortlist plus a mood-question
round. Where a proposal rescues something from those deleted flows — an escape hatch, a reveal
beat, a piece of copy — it says so.

---

## How to read this

Each proposal names the prototype behaviour with a line cite into `Media Graph.dc.html`, quotes the v2.1 sentence it amends, and gives the replacement text in the spec's own register. Proposals that were choices rather than transcriptions carry a **Decided (owner, 2026-08-29)** line naming the answer; the amendment beneath them is written against it. Everything after them is free adoption (copy, constants, states the prototype already worked out) or cosmetic.

Two owner decisions post-date the prototype and are **not** re-litigated here: there is no `forgotten` state, and the Tonight flow uses ~10 candidate votes instead of a shortlist + mood round. Where a proposal recovers something from those deleted flows (an escape hatch, a reveal beat, a piece of copy), it says so explicitly.

"Cost" answers one question: does adopting this change the build order, add a milestone, or is it free text?

---

## §3 — Users, auth, identity

### 1. Where the one-time password lives, and for how long

**What the prototype does.** `addUser()` (2166–2171) generates `pw = 'MG-' + Math.random().toString(36).slice(2,8)`, stores it on the user object, and pushes `user created: {name} · temporary password issued · must change at first login`. The created user then renders **permanently** in the Users list (2949–2952) with the detail string `{role} · temporary password MG-xxxxxx · must change at first login` and an ember `pending first login` badge (987–989). There is no copy-to-clipboard, no reveal/hide, no reissue, no expiry.

**What the spec says.** §3.1: "User creation (wizard or §6.6): a **one-time password** is issued, the account is locked to a password change at first login, and passkey registration is prompted afterwards." §6.6 Users repeats the sentence. Neither says how the password is delivered, whether it is stored, or how long it is visible.

**Proposed amendment.** Replace the §3.1 sentence with: "User creation (wizard or §6.6) issues a **one-time password**: it is displayed once, at creation, with a copy control, and is stored only as an argon2 hash with an expiry (default 72 h) — it is never rendered again in any list or detail view. The account is locked to a password change at first login and carries a `pending first login` state until that change lands; an admin may **reissue** (invalidating the outstanding OTP and restarting its window) but never re-read one. Passkey registration is prompted immediately after the forced change." Every rule above is admin-surface behaviour, so §6.6 carries the same rule on its own page: replace the last sentence of the §6.6 Users bullet with "Creation issues a one-time password (§3.1): it appears once, in the creation confirmation, with a copy control, and never again in the roster or in any user detail view — a not-yet-activated account shows only a `pending first login` badge. That row offers **Reissue**; no control anywhere re-reads an issued one." Without the second half an implementer reading §6.6 alone can still build the prototype's persistent-plaintext roster row.

**Cost.** Free copy plus the two credential columns it implies (`user.otp_hash`, `user.otp_expires_at`), which §4.2's `user(…)` row does not yet carry and M0's auth work needs. The expiry is a tunable default, not a measured constant. The prototype's persistent-plaintext row is the natural but wrong reading of a "one-time" password; landing this late means a data-model change.

### 2. The profile / account page has no section

**What the prototype does.** The account chip menu's first row, `Account & passkeys` (58; phone 1204), has hover styling and **no `onClick`** — every sibling row navigates. There is no profile surface anywhere: the eight surfaces are `library / rate / watch / rank / explore / taste / admin / setup` (2763–2764). No passkey registration, no PIN set/change, no password change, no session list, no push-permission control, no model-log toggle.

**What the spec says.** §3.2: "Registration from the **profile page**; multiple passkeys per user (phone + desktop)." §6.7: "A per-user toggle (default off) reveals an ephemeral log." §6.6 Users covers admin-side passkey management only. No §6 section defines the page both of these point at.

**Proposed amendment.** Add **§6.9 Profile / account page** after §6.8: "The per-user account page, reached from the account chip. Contents: registered passkeys (list with device label, add, remove — §3.2); the optional 4-digit switching PIN (set, change, clear); password change; active sessions and devices with individual revoke; per-device push-notification permission state and a re-prompt affordance (§6 preamble); and the §6.7 model-log toggle. Nothing on this page is admin-gated — it is each user's own record." Every item but two traces to an existing sentence — passkeys, PIN and password change to §3.2, push permission to the §6 preamble, the model-log toggle to §6.7 — open: whether the active-session list with individual revoke and the push re-prompt live here or in §6.6 System.

**Cost.** Adds one small surface to M1 (passkeys land there) with the model-log toggle arriving in M2. No new milestone. The page is spec repair rather than a new commitment: §3.2 already names it ("registration from the **profile page**") and §6.7's per-user toggle presupposes a per-user settings home, so not adding it would mean rewording two normative sentences to point at a host that does not exist (§6.6 is admin-gated and covers admin-side passkey management only).

### 3. Profile switching has no PIN gate and no exit semantics

**What the prototype does.** The menu's `SWITCH USER · DEMO` section (66–71) lists `MEMBERS` minus the current user (2754–2755, `MEMBERS = ['p','j','m']` at 2054 — the guest Sam is excluded). `setUser` (2172) is `this.setState({user: ...})` and nothing else: no PIN prompt, no confirmation, no re-auth. It does not close the menu (`acctOpen` stays true) and does not reset the surface, so switching identity while in Admin lands the new identity in Admin.

**What the spec says.** §3.2: "**Shared devices:** the account chip switches between member profiles, **gated by the per-user PIN** (the chip reads \"member · passkey + PIN\")." The gate is normative; the interaction is undrawn.

**Proposed amendment.** Extend the §3.2 bullet: "Picking another profile opens a 4-digit PIN sheet; the switch commits only on a correct PIN, and a member with no PIN set falls back to password. Committing closes the chip menu and returns to Home — the new identity never inherits the previous identity's surface, and never an admin surface. Persistent guests with a grid profile appear in the switch list; ephemeral guests do not."

**Cost.** Free copy; the PIN sheet is one component in M1's auth work.

### 4. No sign-in, lock, or forced-password-change surface exists

**What the prototype does.** `logout()` (2163) closes the menu and writes the log line `session cookie cleared · passkey remains registered`; it changes neither `user` nor `surface`. There is no login screen, no lock screen, no passkey prompt, no first-login password-change screen, and no PIN entry anywhere in 1–1903 — the app is always already authenticated as Patrick.

**What the spec says.** §3.1: "a **one-time password** is issued, the account is locked to a password change at first login, and passkey registration is prompted afterwards." §3.2: "**Primary: WebAuthn passkeys** … **Fallbacks:** password login (argon2) always available." Both require unauthenticated surfaces that no §6 section owns.

**Proposed amendment.** Add to §6.9 (proposal 2): "The unauthenticated surfaces belong here too: **sign-in** (passkey-first, with a 'use password instead' fallback and a user picker on shared devices), the **forced first-login password change**, and the **PIN sheet** of §3.2. All three are M1 scope and none is drawn in the prototype."

**Cost.** No new milestone — M1 already carries passkeys. Free spec text, three screens to design.

### 5. The wizard's phone-onboarding step is on the wrong device

**What the prototype does.** "Onboard the phones" is step 5 of the **admin's** wizard, rendered on the admin's device (1070–1078), with an ember-highlighted "3 — Enable notifications" row implying the admin completes it. No member first-run route exists anywhere; `wizDone` (2154) is declared and never written or read.

**What the spec says.** §3.1: "**Member first-run onboarding** then walks each phone through PWA install and push permission (§6 preamble)" — i.e. after the wizard, on each member's own device. The §6 preamble makes the device binding load-bearing: "iOS has no programmatic install prompt, so member first-run onboarding *guides* Share → Add to Home Screen, **detects standalone mode**, and nags until push is granted" — neither the detection nor a permission request inside a user gesture can happen on the admin's handset. §12 M2 lists "member PWA-install/push onboarding" separately from M0's wizard.

**Proposed amendment.** Amend §3.1: "The wizard's final step is a **hand-off**, not an install: it shows each created member alongside a QR of `PUBLIC_URL` for their own device, and lists who has yet to complete first-run — members sign in there with the §3.1 one-time password; no additional credential type is introduced. The Add-to-Home-Screen walkthrough and the push-permission request run on each member's own device at their first login (§6 preamble), because neither can be performed on the admin's handset; per-member completion state is visible in §6.6 Users."

**Cost.** Splits one wizard step across two milestones (M0 hand-off, M2 member onboarding). The prototype's step-5 copy survives verbatim — it moves device, not text.

### 6. §3.1's own sequence contradicts "bundle import as the final step"

**What the prototype does.** Five wizard steps, labels `['Create admin','Connectors','Import bundle','Members','Onboard phones']` (2978) — members and phones follow the import.

**What the spec says.** §3.1: "The wizard runs: create admin → optional env-seeded connector config (§2) → **bundle import as the final step** (the same importer the §6.6 Data tab exposes — that one page is M0 scope) → member-account creation (needed before M2 …)." The sentence calls import final and then lists two steps after it.

**Proposed amendment.** Reword to: "The wizard runs: create admin → optional env-seeded connector config (§2) → bundle import, the last of the server-side steps (the same importer the §6.6 Data tab exposes — that one page is M0 scope) → member-account creation (needed before M2, whose exit criterion requires both members' verdicts) → member hand-off (proposal 5)."

**Cost.** Free — an internal spec repair, visible without the prototype.

### 7. The wizard's first step must carry the `PUBLIC_URL` warning

**What the prototype does.** Step 1 (1033–1040) has a heading, the copy "One admin, then members. Passkeys can be added afterwards from the profile page.", a `username` field pre-filled `admin`, and a masked password field. No confirmation field, no `PUBLIC_URL`, no `SECRETS_KEY` mention. The app's only `PUBLIC_URL` warning is stranded in Admin › Users (993): "Passkeys are bound to the public origin. Changing PUBLIC_URL invalidates every registered credential."

**What the spec says.** §14.4: "`PUBLIC_URL` change invalidates passkeys; **wizard warns loudly**." §2: "back up the env file (`SECRETS_KEY`) alongside them, or a restored dump cannot decrypt connector config."

**Proposed amendment.** Add to §3.1's wizard sentence: "Step one also confirms `PUBLIC_URL` and states, in the wizard itself, that changing it later invalidates every registered passkey (§14.4), and that `SECRETS_KEY` must be backed up alongside `pg_dump` output or a restored dump cannot decrypt connector config (§2)."

**Cost.** Free copy; the string already exists in the prototype at line 993 and only needs a second render site.

### 8. Wizard navigation: gating, skip, and a completion action

**What the prototype does.** A fixed footer with `Back` and `Continue` (1081–1084). `wizNext` is `Math.min(4, step+1)`, `wizBack` is `Math.max(0, step-1)` (2531–2532) — both render unconditionally, so `Back` at step 0 and `Continue` at step 4 are inert. **There is no completion action and no exit into the app.** No step validates before advancing; there is no Skip even though step 2 is labelled "Optional now". The wizard is re-enterable at any time from the account menu (62) with nothing tracking which steps were completed.

**What the spec says.** §3.1 gives the sequence and states "a bundle-less app is a legal state", but no gating or completion semantics.

**Proposed amendment.** Add to §3.1: "Only the admin account is mandatory; connector config and bundle import are skippable (a bundle-less app is a legal state) and each step's Continue validates only what it wrote. The final step's action is **Finish** and lands the operator on Admin › Data. Completed steps are recorded per install, so a wizard re-entered from the account menu resumes rather than restarts and never re-issues credentials."

**Cost.** Free copy; the resume state is one column.

### 9. Wizard step 2 should name the three connector row states

**What the prototype does.** Step 2 (1041–1049) copy: "Optional now, changeable later in Admin. Env vars may seed these on first boot for automated installs." Three static rows — Jellyfin with a green `seeded from env` chip and a green-tinted border, LLM providers and TMDB / OMDb / Trakt both grey `skip for now`. Nothing is clickable, so there is no way to configure or to decline explicitly.

**What the spec says.** §2: "env vars may *seed* connector config on first boot for automated installs" — the copy is a near-quote; the rendering is not specified.

**Proposed amendment.** Add to §3.1: "Each connector row in the wizard shows one of three states — *seeded from env* (read-only, overridable in Admin), *configure now* (opens the §6.6 card inline), or *skip for now* — so an automated install and a hand install read the same screen."

**Cost.** Cosmetic; free copy.

### 10. The chip identity line is a live capability summary

**What the prototype does.** The menu header renders `{{ me.role }} · passkey + PIN` (55) as a static string; `me.role` is `member` for p/j/m and `guest` for s (2048–2053), so it renders literally "member · passkey + PIN". Nothing checks whether a passkey or PIN is actually registered.

**What the spec says.** §3.2: "the chip reads \"member · passkey + PIN\"".

**Proposed amendment.** Append to that parenthetical: "— the line is a live capability summary, not a constant: with no PIN set it reads \"member · passkey\", and with neither, \"member · password\"."

**Cost.** Cosmetic; free copy.

### 148. A `guest` account can be created, and §3.1 does not say what one is

**What the prototype does.** The Users tab's create form offers three role pills — `member` / `admin` / **`guest`** (`newUserRoles`, 2954; rendered 1001) — and `addUser` (2166–2171) treats all three identically: a named account with a `MG-` one-time password and `must change at first login`. A guest created this way is therefore a full login-bearing account. Separately, the Users list already asserts a finished guest ("Sam — `guest · grid profile, 11 picks`", `userRows` 2946–2948, rendered 982–991) with no path that produces one, and the Tonight lobby's guests are anonymous `g1…gN` seats on the host's phone (2352) that never touch the account table at all.

**What the spec says.** §3.1: "`guest` (ephemeral or persistent, limited — see §6.2)". §6.2 step 2 describes the ephemeral kind (hand-the-phone); §6.2 and §12 M7 describe the persistent kind's grid. Nothing says a guest is an *account*, whether it can sign in, or what "limited" excludes.

**Proposed amendment.** Add to §3.1: "Two guest kinds, one role. An **ephemeral guest** is a session seat only — no account row, no credential, no Ledger; it exists for the length of one Tonight session (§6.2). A **persistent guest** is an account created in §6.6 with a display name and no password and no passkey: it cannot sign in, it is selectable as a Tonight participant and as a §6.5 comparison profile, and it owns exactly one artefact — the 60-title grid profile (§12 M7). Only `member` and `admin` accounts carry a one-time password and a first-login change; issuing one for a guest creates a login that the role is defined not to have."

**Cost.** Free copy, but it removes a role pill from the §6.6 create form and pins what §6.5's "any two profiles — members and persistent guests" is allowed to select.

---

## §4 — Data model

### 11. What happens to `ledger_cutpoints` when the tier set changes

**What the prototype does.** `TIERS = ['S','A+','A','B','C','D','F']` and `SHARES` are class constants (2057–2058). No surface — Admin included (877–1023) — exposes a control to add, remove or rename a tier, and no state key holds a tier-set choice.

**What the spec says.** §6.3: "tiers **F, D, C, B, A, A+, S** (configurable set; learned cutpoints …)". §5.2: "K-level ordered logit, K = **size of the configured tier set** (default 7 … ⇒ 6 learned cutpoints)". §4.2: `ledger_cutpoints(user_id, boundaries float[])` — "learned tier cutpoints; length = |tier set| − 1 (default 6, ordered ascending)". Three sentences assume a configured tier set and none names who configures it.

**Decided (owner, 2026-08-29): per user.** The tier set is a per-user preference, not a household configuration. Everything else below is consequence, not choice: §4.2 already fixes `length = |tier set| − 1`, so a change in K necessarily invalidates that user's `boundaries` rows.

**Proposed amendment.** Add to §4.2 under `ledger_cutpoints`: “The tier set is a **per-user preference**. `ledger_cutpoints` already carries it — the row is keyed `(user_id, kind)` and holds both `tier_set` and `boundaries` — so no new column is needed; what needs stating is the re-initialisation rule. Changing K invalidates that user’s `boundaries`: on save, their cutpoints are re-initialised to the **equal-mass quantiles** of that user’s fitted `s` distribution for the new K (§6.3’s measured quantile shape F 3 / D 7 / C 15 / B 25 / A 25 / A+ 17 / S 8 % is authored for K = 7 and is not defined for any other K), and a Ledger refit is queued for that user alone — tier *edits* are observations and survive the change, tier *boundaries* do not. One user changing their tier set never touches another’s.” The control belongs on the per-user settings page (§6.9, proposal 2), not on §6.6 Admin: a per-user preference does not belong on an admin surface, and §6.6’s Users tab holds accounts while its System tab holds “job health, queue depth, last syncs, backup status, logs”. Warn on save that it discards that user’s learned cutpoints and queues a refit.

**Note.** `ledger_cutpoints` is keyed per `(user_id, kind)`, so the schema permits a different tier set for films and series. That is finer granularity than the decision requires: the settings control sets one set per user and writes it to both kind rows. The per-kind key is retained because §4.1 rule 5 partitions cutpoint *fitting* by kind regardless of how many tier sets a user has.

**Cost.** Free text household-level; per-user adds a schema column. Either way it gives the control a home in §6.6 and adds a refit trigger to §5.3.

### 12. Seen-state demo residue is not a design

**What the prototype does.** `seenState()` (2123–2128) already collapses the third state: `if (s.forgotten.indexOf(id) >= 0) return 'unseen'`. But `SEEN` still carries `forgotten:[29,14]` arrays per user (2056), `card().seenDot` still has a dead amber branch (3021), and `stateBtn` still has an unreachable `v === 'forgotten'` arm (2227) — no `data-v="forgotten"` control exists.

**What the spec says.** §4.2: "state: unseen | seen (owner decision 2026-08-29: no 'forgotten' state)". §7.3: "the plain boolean … (v2.1 — the `forgotten` state is gone)".

**Proposed amendment.** None to §4. Record in this document only, for implementers: the prototype's `forgotten` arrays and branches are mid-migration residue, not a surviving design — the live behaviour already matches the spec.

**Cost.** Free; no spec change.

### 13. Where cover art comes from

**What the prototype does.** Every poster at every size is a placeholder: a diagonal-hatch `repeating-linear-gradient(135deg,#17171a 0 7px,#1e1e22 7px 14px)` under a per-title hue tint at `opacity:.55` and a bottom scrim (`card()` at 3016–3027; sizes 126×189, `minmax(132px,1fr)` at `aspect-ratio:2/3`, 66×99, 88×132, 34×50, 26×38). The sweep card even labels itself "poster full-bleed" (line 220). **No artwork source is named anywhere.**

**What the spec says.** §6.8: "Poster-forward 2:3 cards." §7.1 lists the ported Jellyfin field set. Grepping the spec for poster/artwork/image returns nothing about where cover art comes from.

**Proposed amendment.** Add to §4.1 after the content-spine table: "Artwork: poster and backdrop images are fetched from Jellyfin's image endpoints for owned titles and from TMDB for unowned ones, cached under `/data/cache`, and served at fixed 2:3 crops. A title with no image renders the neutral hatched placeholder rather than an empty box — the placeholder is a designed state (§6.8), not an error."

**Cost.** Free text, but it names an image cache and a fetch path that M0's Library view needs; today the spec ships a poster-forward UI with no poster source.

---

## §6 — preamble

### 14. Every surface needs a URL

**What the prototype does.** `setSurface` (2160) sets `{surface, sel:null, acctOpen:false}`; all eight surfaces are `sc-if` branches on one string. There is no URL, no history, no deep link, no back button, no per-surface scroll restoration (`grep -c "pushState|history\."` = 0).

**What the spec says.** §6.2 step 8: "Optional **TV kiosk route** (`/tv`, room code)". §11: "a webhook/service to launch a watch-now session from an HA dashboard (**returns the session URL/room code**)". §2: `PUBLIC_URL`. §6 line 209 already fixes the names the routes derive from: "Surface names (prototype, normative): **Home / Rate / Tonight / Rank / Map / Taste** (+ Admin)". Real URLs are required and never enumerated.

**Proposed amendment.** Add to the §6 preamble: "Every surface is linkable, and the route names follow the normative surface names above: `/` (Home), `/rate`, `/tonight`, `/tonight/{code}`, `/rank`, `/map`, `/taste`, `/admin`, `/tv`, plus `/setup` (admin-gated; re-entering it resumes rather than restarts, §3.1) and `/me` (§6.9). §6.2's room-code joins and §11's returned session URL are the same route with a code segment. Selection is URL state: a title opens as `?title={id}` and a person filter as `?person={id}`, so 'Show on map' and 'filter to their filmography' produce shareable links."

**Cost.** Free text; SvelteKit gives file-based routes for nothing. The table is transcription — the surface names are already normative and `/tv` is already in §6.2. The one substantive clause is selection-as-URL-state, which decides whether "Show on map" and the person filter are shareable at all; adopting it late means retrofitting deep links into a single-string navigator.

### 15. Three shell states nobody has drawn

**What the prototype does.** The only PWA content in the whole file is wizard step 5 (1070–1078) plus the footnote at 504. The running shell has no install banner, no standalone-mode indicator, no notification-permission control, no offline/service-worker state, and no "no bundle imported" treatment.

**What the spec says.** §6 preamble: "installable, service-worker shell cache … nags until push is granted." §3.1: "artifact-dependent surfaces render an explicit \"no bundle imported\" state instead of erroring."

**Proposed amendment.** Add to the §6 preamble: "Three shell-level states: (a) a re-entrant **install/push nag** for a member who has not completed first-run onboarding, dismissible per session and re-armed at each login; (b) an **offline/degraded** indicator consistent with the service-worker shell cache, stating which surfaces are readable from cache and which are not; (c) the **\"no bundle imported\"** state of §3.1, which is shell-level rather than per-surface because it affects Home, Rate, Rank, Map and Taste simultaneously and must route to §6.6 Data."

**Cost.** (a) and (c) are already implied by §3.1/§6; (b) is genuinely new and is one component. No milestone change.

### 16. Breakpoints and the middle range

**What the prototype does.** The root is `display:flex;height:100vh;overflow-x:auto` (34) with a `min-width:900px` desktop column (36) beside a 380px phone artboard (1184). There are **zero** `@media` or container queries in the file; the phone tree is ~720 hand-authored lines (1183–1901).

**What the spec says.** §6 preamble: "responsive PWA, phone-first (48 px targets, one-handed, swipe), desktop as progressive enhancement." No breakpoint is named and tablet behaviour is undefined.

**Proposed amendment.** Add: "Two layouts, one breakpoint: the compact layout (bottom tab bar, full-screen detail, bottom sheets) below 900 px, the wide layout (left rail, side detail panel) at and above it. Tablets take the wide layout in landscape and the compact one in portrait. The prototype's two artboards are those two layouts, not two products."

**Cost.** Free text; settles a question every surface would otherwise answer differently.

### 17. Name the surface once: Map, not Explore

**What the prototype does.** The rail renders five items with `data-v` `library / rate / watch / rank / explore` and labels Home / Rate / Tonight / Rank / **Explore** (84–103); the phone tab bar repeats it (1893). The dead binding `phoneTabsOld` (2989) still spells the sixth label **MAP**.

**What the spec says.** §6: "Surface names (**prototype, normative**): Home / Rate / Tonight / Rank / **Map** / Taste (+ Admin)"; §6.4 is titled "Map (exploration)". The parenthetical "(prototype, normative)" is factually wrong about the prototype for this one name.

**Proposed amendment.** Change the parenthetical to "(normative; the prototype's rail says \"Explore\" — the spec name wins)" and append: "Internal route keys are `home / rate / tonight / rank / map / taste`; the prototype's `library / watch / explore` keys are working titles."

**Cost.** Cosmetic; free.

### 149. Taste is not in the navigation on either form factor

**What the prototype does.** The desktop rail carries **five** destinations — Home / Rate / Tonight / Rank / Explore (84–103) — and the phone tab bar carries the same five (1876–1896). Taste is reachable only from the account chip menu, as the row "My Taste" (59 desktop, 1205 phone), sitting between `Account & passkeys` and the admin rows. The phone tab bar then mislabels its own state: the fifth tab is wired `data-v="explore"` with the label "Explore" but takes its active styling from `phoneTabSt.taste`, whose rule is `k === 'taste' ? surface === 'explore'` (1893; 2986–2988) — so the tab lights for Explore under Taste's key, and Taste itself can never light. The dead sibling `phoneTabsOld` (2989–2992) still lists six destinations, including a `TASTE` tab.

**What the spec says.** §6: "Surface names (prototype, normative): Home / Rate / Tonight / Rank / Map / **Taste** (+ Admin)" — six peers, one of them Admin-gated. Nothing says where each is reached from.

**Proposed amendment.** Add to the §6 preamble: "Five destinations are primary navigation — Home, Rate, Tonight, Rank, Map — and appear in the rail and the compact tab bar in that order. **Taste is reached from the account chip**, not from primary navigation: it is a two-profile comparison read occasionally, not a daily surface, and a sixth tab costs every other tab its touch target. It keeps its own route (proposal 14) and is a first-class surface everywhere else. Admin and the wizard are also account-chip destinations, role-gated (proposal 115)."

**Cost.** Free copy. It also settles the tab bar's key/label mismatch, which today ships a five-tab bar whose fifth tab is internally named for a surface it does not open.

---

## §6.0 — Home & Library

### 18. A person filter collides with the kind partition

**What the prototype does.** `lib` filters `t.kind === st.kind` **first** (2576), then `personFilter` (2577). Tony Gilroy is credited on *Michael Clayton* (movie, id 13, line 1971) and *Andor* (series, id 35, line 1993); tapping his name from *Michael Clayton* with the Films pill active shows one title and gives no hint the other exists. Gilroy is the **only** cross-kind credit in the 38-title fixture (`PEOPLE`, 2004–2043): Villeneuve's five titles (ids 2, 5, 6, 12, 20) are all films, Yeun's two (23, 24) are films, Skarsgård's two (33, 35) are series. The fixture therefore hides the collision rather than exhibiting it — at catalog scale, where directors, writers and actors routinely cross kinds, the cross-kind credit is the common case.

**What the spec says.** §6.0 demands both "partitioned by kind (§4.1 rule 5)" and "credits, each person tappable → **filters the library to their filmography**". §4.1 rule 5: "every ranking surface partitions by it."

**Decided (owner, 2026-08-29): the partition is not suspended — the control becomes two toggles.** Neither offered branch was taken. Kind stops being a one-of-two switch and becomes **two independent toggles, Films and Series, either or both active**. A person filter then needs no special case: with both toggles on, a filmography is complete.

**Proposed amendment.** Replace §6.0’s kind control: “Kind is **two toggles — Films, Series — either or both active**, never neither: deselecting the last active one re-selects the other. A person filter does not change them; with both active a filmography is complete without a special case, and with one active the count line says how many the other holds (‘6 films · 2 series hidden’).”

And amend §4.1 rule 5, which the two-toggle control does *not* violate but does need to be read precisely: “…and every ranking surface partitions by it. Selecting both kinds is a selection, not a merge: a surface that **ranks** — Rank, Tonight, the Home shelves — renders two headed sections and never one interleaved ranking, because the measured failure is a *shared ranking* (the unpartitioned crowd top-10 is 8/10 TV series), not a shared screen. A surface that merely **lists** in a kind-independent order — the catalog, sorted by year or title — may interleave freely.”

**Cost.** Free copy plus a small API change: `kind` becomes a set rather than a scalar on the catalog route. M0 scope, and worth doing now — retrofitting a scalar into a set after three surfaces consume it is the expensive version.

### 19. The model line on the title card is unconditional

**What the prototype does.** The card's last block is `<sc-if value="{{ showModel }}">` (1172–1176) containing `{{ sel.blend }}` (= `b(t) 0.517 · β 0.8 · gate 0.93`, built at 3042) and `σ ±0.09 · support n=4213`. `showModel` is initialised `false` (2141) and **no handler anywhere sets it** — the block is dead in every state. The phone card omits it entirely. The same flag also gates two annotations the spec never places behind a toggle: the ledger score and σ under each tier poster in Rank (348–350) and the group score on each Tonight candidate (565–567). `notModel` is exported at 2748 and bound nowhere, so no control exists to flip any of the three.

**What the spec says.** §6.0 lists it unconditionally as part of the M0 card: "and the **title detail card**: metadata; … **the model line in the data voice** (`b(t) 0.52 · β 0.8 · gate 0.93`)". §6.7: "A per-user toggle (**default off**) reveals an ephemeral log". §6.8: "model numbers appear in the data voice next to their name … never bare."

**Proposed amendment.** Resolution already implied by both sections — add to §6.0: "The model line ships **always visible** on the title card; it is the M0 transparency promise and predates the §6.7 rail." §6.7's toggle has never governed anything but the log, so it does not reach the card. Whether that toggle *also* governs the inline annotations the prototype attached to the same flag — ledger score ± σ on tier posters, group score on Tonight cards — is the open question, and it is decided once, at proposal 117.

**Cost.** Free copy; it clarifies a prototype bug rather than settling a fork. Left unstated, implementers gate the M0 promise behind a default-off preference.

### 20. Home has no degraded states

**What the prototype does.** Home always renders shelves or a grid over `this.titles`, a static 38-title fixture built at construction (2114–2122). There is no branch for an empty catalog, an un-imported bundle, or a first-run user with zero verdicts — shelves, tiers, ledger weights and model numbers all exist from the first frame. The only acknowledgement is the wizard caption "first boot · a bundle-less app is a legal state" (1031).

**What the spec says.** §3.1: "artifact-dependent surfaces render an explicit \"no bundle imported\" state instead of erroring." §12 M0 exit: "Library list and title card render imported titles."

**Proposed amendment.** Add to §6.0: "Home has two degraded states. **No bundle imported:** the catalog is empty, shelves are suppressed, and a single explicit panel points at §6.6 Data. **Bundle imported, zero verdicts** (the whole M0→M2 window): tier badges, ledger weights and every score-ordered shelf are meaningless, so Home falls back to the catalog grid plus a route into the §6.1 seed-list queue. Both are first-week states; neither is optional."

**Cost.** Free copy; the zero-verdict fallback is real M2 work that §12 currently hides inside "Home shelves".

### 21. Adopt the pending-verdicts banner copy verbatim

**What the prototype does.** Desktop (113–119): an ember-tinted banner with a bell icon — "You watched {names} — a quick verdict keeps your profile sharp." with a filled pill **Rate now**. Phone (1282–1287): "Watched, not rated: {names}" with a pill **Rate**. `pendingList.names` is a comma-joined list capped at 3 titles (`.slice(0,3)`, 2778).

**What the spec says.** §6.0: "a **pending-verdicts banner** (\"You've watched *X* and *Y* recently — rate them?\" → the §6.1 queue; §7.3's capture prompt given a permanent surface)".

**Proposed amendment.** Replace the placeholder copy: "a **pending-verdicts banner** — wide: \"You watched {titles} — a quick verdict keeps your profile sharp.\" (CTA \"Rate now\"); compact: \"Watched, not rated: {titles}\" (CTA \"Rate\"). At most three titles are named; beyond that the list reads \"{title}, {title} and N more\". The CTA enters the §6.1 queue with those titles at its head."

**Cost.** Free copy.

### 22. Pin the greeting's four time bands

**What the prototype does.** `greeting` (2785) = `(hours<5 ? 'Up late, ' : hours<12 ? 'Good morning, ' : hours<18 ? 'Good afternoon, ' : 'Good evening, ') + USERS[u].name`, rendered at 20px/700 (121) and 15px/700 on phone (1289).

**What the spec says.** §6.0: "A greeting;".

**Proposed amendment.** "A greeting in four bands against §2's `TZ`: before 05:00 \"Up late, {name}\", before 12:00 \"Good morning, {name}\", before 18:00 \"Good afternoon, {name}\", otherwise \"Good evening, {name}\"."

**Cost.** Free copy. "Up late" is house voice worth keeping.

### 23. The catalog empty state teaches DNA search

**What the prototype does.** Above the grid, `libCount` = `lib.length + ' titles'` in mono (133, 2775). `libEmpty` (2775) renders a centred block: **"Nothing in the library matches."** and, in mono, **"try a DNA term — cosy, dread, slow-burn — or check Explore's compositional search"**. The phone grid has neither the count nor the empty state and caps at 8 results (`phoneLib: lib.slice(0,8)`, 2985).

**What the spec says.** §6.0 specifies the search fields and the grid switch but no empty state; §6.4 handles only empty *predicates*.

**Proposed amendment.** Add to §6.0: "The catalog grid carries a result count in the data voice (`N titles`) and, when a query or person filter matches nothing: \"Nothing in the library matches.\" over \"try a DNA term — cosy, dread, slow-burn — or check the Map's compositional search\". Both appear on every viewport. This is the only place the app teaches that DNA terms are searchable, and it is the hand-off into the §8.4 flywheel path."

**Cost.** Free copy.

### 24. Shelf 1: state the anchor rule, and fix the why-line

**What the prototype does.** `homeRows` shelf 1 (2536–2547): anchor = the highest-scoring **seen** title of the active kind, falling back to the top-scoring title; membership = titles sharing **≥2** terms with the anchor, minus the anchor, sorted by personal score, capped at 12. Title = `'Because you put ' + anchor.title + ' in ' + tierOf(u, anchor)`; why-line = `'shares ' + anchor.terms.slice(0,2).join(' + ') + ' with it'`.

**What the spec says.** §6.0 table row 1 gives only the templates: "Because you put *{anchor}* in {tier}" / "shares {term} + {term} with it".

**Proposed amendment.** Add under the table: "Shelf 1's anchor is the user's top-scoring **seen** title in the active kind; membership is ≥2 shared DNA terms; ordering is by ledger score. The why-line must name terms **every** item on the shelf carries — the prototype names the anchor's first two terms while admitting members on any two shared terms, so a card can be shown under a reason it does not satisfy. A shelf that cannot say why it exists doesn't ship; nor does one that says the wrong why."

**Cost.** Free text; it turns a shelf-construction bug into a stated constraint.

### 25. Say whether score-ordered shelves exclude seen titles

**What the prototype does.** Shelf 2 "Top of your ledger" is `byScore.slice(0,12)` (2550) with **no seen filter**, so on a real library it fills with rewatches. Shelf 5 "Under 110 minutes" is `pool.filter(t => t.runtime < 110)` (2559), also unfiltered.

**What the spec says.** §6.0 row 2: "Top of your ledger" / "clean item prior + your fold-in, blended at β 0.8". Nothing about seen state.

**Proposed amendment.** Add under the table: "Unless a shelf's why-line says otherwise, shelves exclude titles the user has already seen; 'Top of your ledger' is the one exception and says so ('your highest, rewatches included')."

**Cost.** Free copy; one predicate per shelf.

### 26. Define `{other}` for more than two members

**What the prototype does.** `const other = this.MEMBERS.filter(m => m !== u)[0]` over `MEMBERS = ['p','j','m']` (2542, 2054). Patrick → Jenny, Jenny → Patrick, **Mia → Patrick always**. Mia never gets a Jenny shelf; there is no partner picker and no second shelf.

**What the spec says.** §6.0 row 4: "You and {other} both rate these highly". §3.1: "membership is open-ended, a third member and persistent guests are first-class throughout."

**Proposed amendment.** "With more than two members, `{other}` is the member with the most co-seen titles — the same set the shelf already ranks over, so it costs no extra read — ties broken by the most recent co-seen title. The shelf's chip lets the user swap to any other member or grid-profiled guest, and the choice persists; at most one such shelf ships per Home render." The default is a starting point rather than a constraint: the chip makes any other reading (most recent co-watch, a pinned partner) one tap away, which is why the default does not need to be adjudicated before the section is written.

**Cost.** Free copy; one query over data the shelf already reads. Left undefined, a third member silently never appears.

### 27. "Under 110 minutes" misbehaves on the Series tab

**What the prototype does.** The filter is applied to series too, where `runtime` is minutes **per episode** — `card()` renders it as "45 min/ep" (3019) and the fixture's series run 27–60 min (e.g. *Andor* 45, line 1993). On the Series tab the shelf is nearly the whole catalog under a misleading title.

**What the spec says.** §6.0 row 5: "Under 110 minutes" / "for a school night".

**Proposed amendment.** "Under the Series partition this shelf restates itself as **\"Episodes under 45 minutes\"** with the same why-line; the thresholds (110 min film, 45 min episode) are constants, not copy."

**Cost.** Free copy.

### 28. The shelf-row interaction contract

**What the prototype does.** `rowWheel` (2195–2199) converts vertical wheel into `scrollLeft` when `|deltaY| > |deltaX|` — without `preventDefault()`, so the page scrolls too. `rowNudge` (2200–2204) pages by `clientWidth * 0.8` with `behavior:'smooth'` from two 42px gradient gutters that are `opacity:0` until hover (185–190). The phone row (1316, 1331) has neither: native momentum scroll, a 34px right-edge fade `linear-gradient(90deg,rgba(13,13,15,0),#0f0f11)`, scrollbars hidden via `data-nobar`. Shelves cap at 12 items with no "see all", and rows with ≤2 items are dropped entirely (2564).

**What the spec says.** Nothing. §6 preamble's "desktop as progressive enhancement" is the nearest text.

**Proposed amendment.** Add to §6.0: "Shelf rows page horizontally. Pointer devices get wheel-to-horizontal with axis detection plus hover-revealed edge chevrons paging 80% of the viewport; touch gets native momentum scroll with an edge-fade affordance and no chevrons. Each shelf holds at most 12 titles and ends in a 'see all' card that opens the catalog grid pre-filtered to the shelf's predicate; a shelf with fewer than three members is suppressed rather than shown short."

**Cost.** Free text; the "see all" card is small new work that turns a hard cap into a navigable one.

### 29. Shelf-card chrome is part of the shelf spec

**What the prototype does.** `rowCard` (2190–2194) returns `rank: i+1`, `tier: tierOf(u,t)` and a `seenSt` dot. The poster (173–178) renders the rank number top-left in mono 15px at 30% white and, top-right, a 7px seen dot beside a mono tier chip on `rgba(13,13,15,.7)` with a hairline. The phone card keeps rank and tier, drops the dot (1322–1323); catalog cards keep the tier chip (149).

**What the spec says.** §6.0 specifies shelves and why-lines but nothing about card chrome; §6.3 owns tiers.

**Proposed amendment.** Add to §6.0: "Every shelf card carries three overlays: its rank within the shelf (top-left), and the seen dot plus tier badge (top-right). The tier vocabulary of §6.3 is therefore ambient on Home; §6.3's straddle and tension badges do **not** appear on shelf cards — the shelf card shows the settled tier only."

**Cost.** Free text; it prevents each surface inventing its own badge set.

### 30. Record the person-filter chip's side effects

**What the prototype does.** `pickPerson` (2179) sets `{surface:'library', personFilter:name, sel:null, q:''}` — it navigates to Home, closes the detail card, and clears the search box. The chip renders as an ember pill `{personFilter} ×` and is itself the clear control (`clearPerson`, 2180). The match is exact full-name against a flat per-title people list (2577).

**What the spec says.** §6.0: "credits, each person tappable → filters the library to their filmography"; "Search or an active person-filter switches Home into the catalog grid".

**Proposed amendment.** Append: "Tapping a credit navigates to Home, closes the title card, and clears the search box; the person chip is itself the clear control. Matching is by `person.id`, never by name string."

**Cost.** Cosmetic; free. (The name-string match is a fixture artefact — `credit` has ids.)

### 31. The platform-scores caption must travel with the number

**What the prototype does.** A dashed block (1135–1141) shows `sel.imdb` labelled "IMDb" and `sel.rt` labelled "critics" over the mono caption "display-only schema — platform scores are a popularity conduit and are never model features". The **phone card shows the IMDb number as a bare stat tile with no caption** (1239).

**What the spec says.** §6.0: "platform scores (display-only schema, **labelled as such**)". §4.1 rule 3 supplies the reason.

**Proposed amendment.** "…labelled as such, with the caption \"display-only schema — platform scores are a popularity conduit and are never model features\". The label travels with the number on every viewport; a platform score never renders bare."

**Cost.** Free copy.

### 32. Kind tabs: labels and the detail-clearing side effect

**What the prototype does.** Two pills from `kindTabs` (2769–2771), values `movie`/`series`, labels **"Films"/"Series"**, default `movie` (2142). `setKind` also clears the open detail (`sel:null`, 2173). Both shelves (`homeRows(u, st.kind)`) and the catalog grid filter on kind first (2535, 2576).

**What the spec says.** §6.0: "partitioned by kind (§4.1 rule 5)"; §5.1: "Films and series ranked as **separate surfaces**".

**Proposed amendment.** Append to §6.0: "The partition control is labelled **Films / Series** (not Movie / TV) and switching it closes any open title card, because the card's tier and ledger weight are per-kind quantities."

**Cost.** Cosmetic; free.

### 33. Name the σ / support line on the card

**What the prototype does.** The model block's second line is `σ {{ sel.sig }} · support n={{ sel.n }}` (1174), where `sig = '±' + t.sigma.toFixed(2)` and `gate = (t.n/(t.n+10)).toFixed(2)` (3023–3024) — exactly §5.1's evidence gate at k=10.

**What the spec says.** §6.0 quotes only the first line (`b(t) 0.52 · β 0.8 · gate 0.93`). §5.1 and §5.2 define both quantities.

**Proposed amendment.** Extend §6.0's model-line clause: "…and beneath it `σ ±0.09 · support n=4218` — the only place per-title crowd support is legible, and what makes the 'New in the library' shelf's claim checkable."

**Cost.** Cosmetic; free.

### 150. The pending-verdicts banner is not §7.3's finish prompt

**What the prototype does.** `hasPending` / `pendingList` (2778–2780) select titles the user has **already** marked `seen` and never given a verdict, capped at three, and render the §6.0 banner (113–118 desktop, 1282–1286 phone). Nothing anywhere watches playback, and the string "Did you finish" appears nowhere in the file. The banner's CTA, `goRate` (2185), sets `{surface:'rate', sel:null, rateMode:'sweep'}` — it does not touch `qi` and does not seed a queue, so the banner names three titles and then presents whatever card the standing queue position holds.

**What the spec says.** §6.0 calls the banner "**§7.3's capture prompt given a permanent surface**". §7.3 defines that capture prompt as a different event: "≥90% playback (poll `/Sessions` + `IsPlayed` delta) **arms a per-user prompt** — \"Did you finish X?\" → one tap sets `seen` and offers the verdict flow… The banner path is the whole M1 behaviour."

**Proposed amendment.** Separate them in §6.0 and §7.3. "Two prompts, not one. §7.3's **finish prompt** is armed by a playback event, names one title, and its first tap writes `seen` — it is a push notification, or, when undeliverable, an in-app card on next open. §6.0's **pending-verdicts banner** is a standing Home element over titles that are already `seen` and carry no verdict, whatever set them so; it never writes `seen` and it names up to three. A finish prompt answered with 'yes' leaves a title in the banner's population until a verdict lands." Then, in both: "The CTA enters the §6.1 queue **with the named titles at its head** (proposal 21) — a prompt that names titles and then presents a different one is worse than no prompt."

**Cost.** Free copy; it splits one M1 line item into the playback watcher (M1) and the Home banner (M2), which §12 already funds separately.

### 151. Salience and source are what make an evidence quote checkable

**What the prototype does.** Each extracted DNA card renders three things: the qualified term, `sal {n}` right-aligned in the header, the quote in italics, and beneath it a mono provenance string — `metacritic:critic` or `trakt:comment` (1150–1157; produced at 2098–2099, `salience: 1 + round(h*2)` ⇒ ∈ {1,2,3}). The phone card (1263–1267) renders the term and the quote and drops both the salience and the source.

**What the spec says.** §4.1 rule 1: "`dna_evidence` ships with the extracted tier — **a tag without its quote is unfalsifiable**." Rule 2: "`salience`, `confidence`, `n_sources` are **weights, never filters**." §6.0 requires "tags with evidence quotes, extracted/projected tier visibly distinct". Neither says the weight or the quote's origin is displayed.

**Proposed amendment.** Extend §6.0's DNA-card clause: "…each extracted tag shows its salience in the data voice (`sal 2`, the 1–3 scale of §8) and the **source of its quote** (`metacritic:critic`, `trakt:comment`) beneath the quote itself. A quote with no origin is only half-falsifiable; the origin is what lets a reader go and check it. Both travel to the compact layout — they are the smallest text on the card, not the first to be cut."

**Cost.** Cosmetic; free copy. It also makes §4.1 rule 2 legible: a number shown next to a tag that is visibly not filtering anything is the rule stated in the interface.

### 152. Home's decade and seen-state filters have handlers and no controls

**What the prototype does.** `state.decade` and `state.seenFilter` (2142) exist, `setDecade` and `setSeenFilter` (2175–2176) are working handlers, and both are exported from `renderVals` (2774). Both predicates are already wired into the catalog query (2579–2580) and can never be non-empty, because **grep of the whole 1903-line template finds zero bindings for any of the four**. The catalog's only controls are the search box, the Films/Series pills and the person chip; the search predicate itself (2578) matches title, genre and DNA term — never alias.

**What the spec says.** §6.0 / §12 M0: "a paginated list over `title`, partitioned by kind (§4.1 rule 5), **filter/search on title/alias/genre/decade/seen-state**".

**Proposed amendment.** Add to §6.0: "The catalog's filter set is one search field matching **title, alias, genre and DNA term** plus two structured controls — decade and seen-state — and the kind partition. Alias matching is not optional: it is the only way a title the household knows by another name is findable, and §4.1's alias table exists for it. Active filters render as removable chips beside the person chip, and the result-count line states them."

**Cost.** Free copy; the controls are M0 scope §12 already claims. Today an implementer copying the prototype ships three of the five named dimensions and leaves two as unreachable state.

---

## §6.1 — Rate

### 34. No model-derived signal on a battle card

**What the prototype does.** `paMeta`/`pbMeta` (2801) = `{year} · {tierOf(user, title)}`, rendered under each poster (282, 286; phone 1386, 1390) — e.g. "1995 · A" vs "2013 · B". `tierOf` (2130–2135) reads the user's own fitted position, so every duel displays the model's current belief about both titles *before* the answer.

**What the spec says.** §6.1 scopes the anchoring rule to sweep only: "Prediction reveal strictly *after* the tap (anchoring; Cosley 2003)" — stated with its research citation, i.e. as a finding rather than a taste. The Battle bullet is silent on anchoring but enumerates the card exhaustively — "two posters are the buttons; `Tie` …; a persistent **decisive toggle** …; Corrections zone at the bottom (nothing tappable inside the poster cards), one row" — and licenses no tier badge.

**Proposed amendment.** Add to §6.1 Battle: "The anchoring rule extends to battles: **no model-derived signal — tier, score, σ, rank — is visible on a battle card before the answer.** Year, runtime and genre only. Tiers may appear in the post-answer state. As drawn in the prototype, every duel is anchored on exactly the quantity it exists to correct."

**Cost.** Free copy; removes two bindings. If tiers are wanted on the battle card, that is a deliberate exception to the §6.1 anchoring rule and must ship with its reason; absent one, the rule applies.

### 35. Undo must pop any observation, one block deep

**What the prototype does.** `undo` (2232–2237) returns early unless `lastAction` is set, and **only `verdict` sets it** (2220). `skip`, `stateBtn` (not seen), `duel` (including `TIE`, which is a `data-v` routed through the same handler, 2244–2249) and `correct` are all irreversible. The chip is never removed, only dimmed to 28% (`undoSt`, 2799). Worse, `undo` blindly does `qi: max(0, qi-1)`, so verdict → skip → Undo deletes the earlier verdict while landing the user on the skipped title.

**What the spec says.** §6 preamble: "**undo everywhere**". §6.1: "+ persistent Undo"; corrections are "covered by the persistent Undo".

**Proposed amendment.** Add to §6.1: "Undo pops the last observation of **any** kind — verdict, not-seen, skip, duel, tie, correction — restores the exact card or pair that produced it, and emits the compensating model-log line (§6.7)." This is not a new rule: §6 already binds every surface to "undo everywhere" and §6.1 already places the corrections row under the persistent Undo, neither of which a single verdict slot can satisfy. **Decided (owner, 2026-08-29): one block.** Undo reaches back to the start of the current block of 15 and no further — the journal is bounded by the block, the depth matches the counter the user is already reading (“7 / 15 this block”), and the chip disables visibly, not silently, at the block boundary. Starting a new block commits the previous one.

**Cost.** Real M2 work: an observation journal with compensating writes rather than a `lastAction` variable. Decide it before the Ledger write-path is built, not after.

### 36. Mix must open in Mix, and Mix must actually alternate

**What the prototype does.** Initial state is `rateMode:'sweep'` (2143) and the Home banner entry `goRate` (2185) also forces `'sweep'`, so **Mix is never the state the user lands in**. `showSweep`/`showBattle` gate on `qi % 2` (2791–2792), but `qi` is advanced only by `verdict` (2219) and `next()` (2230) — `duel` increments `bi`/`duels` (2248) and `correct` increments `bi` (2255). Once Mix flips to odd `qi`, **no number of duels ever returns the user to a sweep card**.

**What the spec says.** §6.1: "**Modes:** **Mix** (default — alternates sweep and battle), Sweep, Battle; blocks of 15."

**Proposed amendment.** Add: "Every entry point — nav, pending-verdicts banner, deep link — opens in Mix; mode is sticky per user only after an explicit change. In Mix, one **observation** (verdict, not-seen, skip, duel, tie) advances the block counter and flips the card type; a corrections-row tap redraws the pair without advancing."

**Cost.** Free copy; it defines the step machine the prototype gets wrong.

### 37. Queue construction, exclusions, and a drained state

**What the prototype does.** `queue()` (2211) is every title of the active kind sorted by `hash(title + 'q')` — a stable pseudo-shuffle. It does not order by P(seen), does not consult seen state, does not exclude titles already given a verdict, and is indexed `q[qi % q.length]` (2586) so it **wraps forever**: no exhaustion state, and a title can be re-served and re-rated.

**What the spec says.** §6.1: "**Queue:** P(seen)-ordered (Jellyfin history, popularity, household co-seen), seeded first run from the imported 100-title decade-stratified `seed_list`."

**Proposed amendment.** Extend that bullet: "Titles already carrying a verdict, and titles skipped in this session, are excluded. When the queue drains, the surface shows an explicit end state — \"You've rated everything we can queue. Battles sharpen what you've already said, and the §6.3 comparison queue sharpens the boundaries.\" — rather than wrapping."

**Cost.** Free copy; one predicate and one empty state in M2.

### 38. `Skip` semantics

**What the prototype does.** `skip` (2231) pushes `skipped — no row written` and calls `next()`. It does not set `lastAction` (so Undo cannot reach it) and does not mark the title, so the same title returns on the next wrap.

**What the spec says.** §6.1: "+ `Skip` + persistent Undo" — nothing about whether skip writes a row or suppresses re-queue.

**Proposed amendment.** "`Skip` writes no observation but records a per-user session suppression so the title is not redrawn in the same sitting; it is covered by Undo like every other action, and it is not a not-seen row (§13's not-seen-rate instrument counts only `unseen` state writes)."

**Cost.** Free copy.

### 39. Give the queue reason a placement

**What the prototype does.** `curWhy` (2796) = `'queued because: ' + round(45 + hash(title+'ps')*52) + '% likely you have seen it'` — the spec's phrasing, computed and **bound nowhere** in 1–1903. The sweep card shows title, meta and logline only; the user is never told why this title is in front of them.

**What the spec says.** §6.1: "each card shows its queue reason (\"queued because: 72% likely you have seen it\")". §6.8: "every shelf, recommendation, question and conflict carries a one-line why".

**Proposed amendment.** Append to that clause: "…rendered in the data voice under the meta line and above the recall aid, naming the dominant reason — P(seen), seed-list position, household co-seen, or pending-verdict carry-over."

**Cost.** Free copy; the string already exists in the prototype and only needs a slot.

### 40. Specify the rating card's payload

**What the prototype does.** The card (220–228) is 620px min-height with a hatched placeholder, per-title hue tint, bottom scrim and the label "poster full-bleed". Content: 40px/700 title; `curMeta` (2794) = `{year} · {runtime} · {genre}`; then `curLogline` (2795) = `cur.dna[0].quote` — the **DNA term's evidence quote**, not a plot logline ("the timeline is a deck of cards, shuffled deliberately"). `curDna` (the top-3 DNA chips) is computed and unbound.

**What the spec says.** §6.1 says only "one title card"; §6.0 specifies the *detail* card, not the rating card.

**Proposed amendment.** Add to §6.1 Sweep: "The rating card carries: poster, title, `{year} · {runtime} · {genre}` in the data voice, the queue reason, and one **recall aid** — a plot logline, not a DNA evidence quote. The user's task on this card is to remember whether they saw it; a facet quote reads well and does not help."

**Cost.** Free copy, plus a logline field the bundle already has room for in `title_meta`.

### 41. The class-balance warning needs a trigger and a `{class}` slot

**What the prototype does.** Copy at 253–255 matches §6.1 verbatim. The trigger is `cbWarn: vs.length > 3 && cnt.liked/tot > .6` (2809) — more than **three** verdicts this session and over 60% "liked". The 60% matches the measurement; the n>3 floor is invented, and the string hardcodes "liked" though the failure mode is class-generic. Counts come from `state.verdicts` filtered to the current user (2587–2588), i.e. **session-scoped**.

**What the spec says.** §6.1 quotes the copy; §5.2 supplies the number: "a 60%-\"liked\" labeller gives up ~0.07 ρ".

**Proposed amendment.** Extend §6.1: "The warning arms after ≥10 verdicts and fires when **any one class** exceeds 60% of the running distribution: \"Heavy on '{class}'. Spreading across all three classes matters about five times more than anything else you can do here.\" The widget shows this session's counts with the user's lifetime distribution as a second, ghosted row — the 5× lever is about the labeller's lifetime shape, and the warning fires on the lifetime share."

**Cost.** Free copy; the lifetime query is trivial. Without it the widget measures the wrong thing.

### 42. Where the prediction reveal lives, and for how long

**What the prototype does.** After a tap, `lastRated.text` (2216–2221) renders in the header row beside the mode pills, mono 11px at 35%, `fadeIn .2s` (214–216). The string is `{title} — you said {label}` plus ` · we'd have guessed the same` / ` · we'd have guessed {class}` — §6.1's copy exactly. Separately, `revealed`/`notRevealed` (2803), `pred` and `predP` (the raw score to 2dp, 2804) and `curDna` (2793) are computed and **bound nowhere**: a dedicated reveal stage was designed and dropped. The header toast is never cleared by skip or not-seen, so it goes stale, and the phone column never renders it at all (1338–1411).

**What the spec says.** §6.1: "Prediction reveal strictly *after* the tap (anchoring; Cosley 2003), phrased \"we'd have guessed the same\" / \"we'd have guessed {class}\"." Placement and duration are unstated.

**Proposed amendment.** Add: "The reveal is attached to the card just rated — it replaces the verdict strip for ~1.2 s or until the next card — never a persistent header slot, and it clears on any subsequent action. It carries the data voice: \"we'd have guessed liked · s 0.71\". It appears on every viewport."

**Cost.** Free copy; recovers `predP`, which the prototype computed and dropped.

### 43. Phone-first means the rail is not optional

**What the prototype does.** The phone Rate surface (1338–1411) reproduces the header, mode pills, sweep card and battle card. It renders **none** of: the class-balance widget, the warning copy, "Where you are", the `decisive` toggle, PAIR SELECTION, RESOLUTION — or the prediction reveal. Every measured-expectation string on this surface, and the only control for margin weight, is desktop-only.

**What the spec says.** §6 preamble: "phone-first (48 px targets, one-handed, swipe), desktop as progressive enhancement." §5.2 requires the class balance and decisive framing be "encoded in UX copy".

**Proposed amendment.** Add to §6.1: "On phones nothing in the rail is dropped: the class-balance widget collapses to a three-segment bar in the header (tap to expand with its warning), the decisive toggle sits inline beside the tie control, and the measured-expectation cards (learning curve, pair selection, resolution) move behind one `why these pairs?` sheet."

**Cost.** Free copy; real phone layout work in M2 that the prototype does not cover.

### 44. Keep the corrections row as specified — the prototype is wrong

**What the prototype does.** `correct` (2251–2256) pushes `unseen: {title|both} → pair swapped, no duel row written` and increments `bi` — and since `pair()` derives **both** indices from `bi` (2238–2243), any tap swaps the **whole pair** regardless of which side was tapped. It does not set `lastAction` (no Undo), writes no state row, and its log line never mentions the Jellyfin write that `stateBtn` narrates.

**What the spec says.** §6.1 is already complete and correct: "one row: `not seen: [left] [both] [right]` → sets that side `unseen`, swaps it out of the pair (`both` swaps the whole pair), writes no duel row, syncs per §7.3, covered by the persistent Undo."

**Proposed amendment.** No change to the rule. Add the log line to §6.7's example set so the implementer has it: "`user_title.state(X) = unseen → Jellyfin Played false · pair half swapped, no duel row written`."

**Cost.** Free; a note, not an amendment.

### 45. Define "within verdict bands"

**What the prototype does.** `pair()` (2238–2243) takes the seen titles of the active kind, then `i = (bi*2) % len`, `k = (bi*2+1+floor(bi/3)) % len` with a collision bump — a fixed deterministic walk, identical for every user, with **no verdict-class filtering**, so an unrated title can be duelled against a disliked one. `bi` is never reset.

**What the spec says.** §6.1: "Pairs drawn **at random** from the user's seen titles **within verdict bands** — no clever selection for profiles (measured null)."

**Proposed amendment.** Expand: "…within verdict bands: the pool is the user's seen titles that already carry a verdict, and pairs are drawn uniformly at random **within** a verdict class. Cross-class pairs re-derive the class boundary the ordered-logit arm already knows; within-class pairs are what add resolution (§5.2)."

**Cost.** Free copy; it makes an under-specified phrase buildable.

### 46. The Rate surface needs its own kind control

**What the prototype does.** Both `queue()` (2211) and `pair()` (2240) filter on `this.state.kind`, which is settable **only** from Home and Rank (`kindTabs` bound at 122, 328, 1290, 1621 — never inside 201–323). A user who rates films and then wants to rate series must leave, flip the toggle elsewhere and come back, with nothing on the Rate surface indicating which partition is being served.

**What the spec says.** §4.1 rule 5: "every ranking surface partitions by it"; §5.1: "separate surfaces". No per-surface control is named.

**Proposed amendment.** Add to §6.1: "The Rate surface carries the film/series partition control in its header and names the active partition in the block counter (`7 / 15 · film · sweep`). The queue and the battle pool never mix kinds."

**Cost.** Free copy; one control.

### 47. Decisive toggle: default and copy placement

**What the prototype does.** A 26×15px pill track next to a mono "decisive" label (306–309), whole card clickable (`toggleDecisive`, 2250), default `false` (2143). The weight appears only inside the model-log string (2246): `· margin decisive (w=1.6)` vs `· margin normal (w=1.0)`. The toggle carries no explanatory copy — the justification lives two cards down in RESOLUTION.

**What the spec says.** §6.1: "a persistent **decisive toggle** sets the margin weight (~1.6 vs 1.0) with the copy \"a decisive pick teaches more than a hesitant one\"".

**Proposed amendment.** Append: "…The toggle defaults **off** and carries that copy on itself as a one-line why; the RESOLUTION card carries the running effect, not the justification."

**Cost.** Cosmetic; free.

### 48. Keep the mirrored `left | tie | right` strip

**What the prototype does.** Two 50% poster panes are `duel` buttons (269–274) under a full-inset scrim with `pointer-events:none` (276). **Below them** a segmented strip: "left" (flex:1), "tie" (fixed 170px, 72% opacity), "right" (flex:1), wired to the same handler with `data-v` A / TIE / B (289–294).

**What the spec says.** §6.1: "**Battle:** two posters are the buttons; `Tie` (feeds the Davidson tie term)". The strip is unmentioned — yet it is the only way to cast a tie and the only thumb-reachable target on a phone.

**Proposed amendment.** "…two posters are the primary targets; a bottom `left | tie | right` strip mirrors them for one-handed use and is where `Tie` lives."

**Cost.** Cosmetic; free.

### 49. Make the learning-curve card show a position

**What the prototype does.** The "Where you are" card (257–260) renders §6.1's learning-curve copy verbatim and is otherwise **inert** — no count, no progress, no position on the curve, despite the heading promising exactly that.

**What the spec says.** §6.1: "The side rail carries the learning-curve copy (\"Personal signal roughly triples from 5 to 100 labels. Aim for 50–100 in the first sitting or two.\")".

**Proposed amendment.** Append: "…plotted against the user's own lifetime label count — the copy is the caption, the position is the point. This is the one place §12's M2 exit criterion (50–100 verdicts each) is legible to the user."

**Cost.** Free copy; one number the widget already has.

### 50. Surface the re-ask and held-out streams in §6.1

**What the prototype does.** Nothing in the Rate handlers implements or marks §13's streams. The Rank surface does model one of them — `cqUniform: cqi % 10 === 9` with the label "uniform-random (held-out 10%) — this pair is never used to tune" (2603, 2815) — but the Rate battle has no equivalent and no verdict or duel is ever re-asked.

**What the spec says.** §13: "a separate silent **re-ask stream** — ~10% of comparisons/verdicts re-asked after ≥3 days; ~200 re-asks measure the flip rate σ that sets the tier budget."

**Proposed amendment.** Add to §6.1 Queue: "~10% of queue slots are silent re-asks of verdicts and duels ≥3 days old — indistinguishable from a normal card by design, never labelled in the UI, and excluded from the class-balance widget."

**Cost.** Free cross-reference; the stream is already mandated "from day one" in §13 and will otherwise be missed at M2.

### 51. Preload and the long-press accelerator

**What the prototype does.** Only `onClick` on posters (269–274) and strip (289–294); no loading state, skeleton, preload or latency treatment anywhere in 201–323, and no long-press handler.

**What the spec says.** §6 preamble: "**<2 s per sweep card, <1.5 s per battle**, undo everywhere, next card preloaded." §6.1: "(long-press stays as an optional accelerator only)."

**Proposed amendment.** Add to §6.1: "The next card or pair is fetched while the current one is on screen; a card must never show a spinner. Long-press on a poster casts a decisive duel directly (equivalent to toggle-on plus tap) and is the only gesture accelerator. Verdict and duel `latency_ms` are captured as §4.2 already does for `session_answer`."

**Cost.** Free copy plus two schema columns; §13 gets a throughput instrument it currently lacks outside Tonight.

### 52. Verdict labels: case and order

**What the prototype does.** Cells in order `data-v="disliked" / "ok" / "liked"` with visible labels **"disliked", "fine", "liked"** — lowercase, worst→best left to right (229–233), JetBrains Mono 15px, 22px vertical padding.

**What the spec says.** §6.1 writes the triple as "`Liked / Fine / Disliked`" — Title-case, descending. §4.2: "value: 0 disliked / 1 ok / 2 liked".

**Proposed amendment.** Add one clause to §6.8: "Verdict labels are lowercase in the data voice and ordered worst→best left to right, matching the stored ordinal."

**Cost.** Cosmetic; free.

### 53. Adopt the "Random pairs." lead-in

**What the prototype does.** The PAIR SELECTION card (310–313) reads: "**Random pairs.** For profiles no selection rule beats random — the clever ones only pay off in the tier queue."

**What the spec says.** §6.1 quotes the second sentence only.

**Proposed amendment.** Prefix the quoted string with "Random pairs." — it turns a defence into a statement.

**Cost.** Cosmetic; free.

### 153. The prediction reveal is banded on the user's own cutpoints

**What the prototype does.** `predV` (2591) is `score(u, cur) > .66 ? 'Liked' : score(u, cur) > .33 ? 'Ok' : 'Disliked'` — two **fixed** CDF cuts, identical for every user, and it is what `lastRated.text` compares the tap against to choose "we'd have guessed the same" or "we'd have guessed {class}".

**What the spec says.** §5.2, verdict arm: "3-class verdicts | **ordered logit, free per-user cutpoints** | monotone link ⇒ a mis-placed personal threshold widens ties but cannot invert an ordering". §6.1 states the reveal's copy and its anchoring rule and never says what produces the predicted class.

**Proposed amendment.** Add to §6.1: "The reveal's predicted class is read off the **user's own fitted cutpoints** (§5.2, §6.3's 'learned cutpoints, not percentile cuts'), not fixed CDF bands. Until a user has enough labels for a fit, the reveal is **suppressed** rather than banded — a guess drawn from someone else's thresholds is not a prediction about this user." Fixed bands are foreclosed by §5.2's verdict arm and §6.3 together; the only thing the spec leaves open is the pre-fit case, which the second sentence settles.

**Cost.** Free copy if the fit is already exposed; otherwise the reveal waits on the Ledger's cutpoint read, which is M2 work either way. Deferring it ships a reveal that can tell a generous rater "we'd have guessed liked" about a title the board files under C — precisely the mismatch §5.2's free cutpoints exist to absorb.

---

## §6.2 — Tonight

### 54. What actually delivers "one of each" on a split axis — *superseded*

**What the prototype does.** On the deleted shortlist screen (`wnFin`, 539–578 — the stage §6.2's v2.1 preamble removes), a contested axis renders a banner of `splitCopy` plus a fixed clause (547–549): "You're split on {facet} — here's one of each. **The axis is zeroed, not averaged — one of each is in the set below.**" The zeroing is real: `finalists()`'s `match()` skips a facet when `Object.values(t[f]).length > 1 && sum === 0` (2421–2425). But **nothing guarantees the promised one-of-each** — zeroing only removes the facet's contribution to ranking; `finalists()` then sorts by `x.g + match(x)` and slices the top 3, which can land wholly on one pole. The copy and the zeroing are recovered from that deleted screen; with the shortlist gone there is no "set below", so the mechanism below is new.

**What the spec says.** §6.2 step 5: a split "is **surfaced with the alternative in hand** (\"You're split on light vs heavy — here's one of each\"), never silently averaged." §0: "a surfaced split must never ship bare." Step 6 fixes the surfaces the alternative could occupy — winner card, runners-up, one wildcard — and constrains how they are ordered: "Votes *choose*; nothing re-ranks within the evening by predicted enjoyment (measured: worth 0.000)."

**Superseded (owner, 2026-08-29).** The owner answered by redesigning the round rather than picking a slot, which makes the original question moot: there is a shortlist stage again, so the alternative has an obvious home. See **§6.2 — Tonight, rewritten** at the end of this document; 54d carries the answer (the third finalist slot is reserved for the opposite pole). The analysis below is kept because its diagnosis still holds — zeroing an axis removes an influence and cannot by itself produce an alternative.

**Proposed amendment.** For (a), add to step 5: "Zeroing the contested axis removes its influence on ranking; it does not by itself produce an alternative. When a split is surfaced, the **first runners-up slot** (step 6) is reserved for the highest-tallied title on the opposite pole of the contested axis, labelled as such. The winner remains whatever the tallies chose — step 6's 'votes choose' rule is untouched, since the reservation orders the runners-up and never the winner. Copy: \"You're split on {facet} — here's one of each. The axis is zeroed, not averaged.\"" For (b) the same sentence names a second card instead of a runners-up slot, and step 6's winner-card inventory grows by one element.

**Cost.** Free copy, but it adds a construction step to the M4 combine and, under (b), an element to the winner card that has no prototype (proposal 58). Without it the app promises something the ranking may not deliver — the exact failure §0's surfacing rule was written to prevent.

### 55. Solo lands on the picks, not on a question round

**What the prototype does.** `wnSolo` (2305–2309) sets `{step:'mood', solo:true}`, so the "Just me" door enters the mood-question round; `wnAnswer` reaches the solo picks only once `qi >= qs.length` (2390). The mood screen (509–537) offers A / B / "Neither pulls me tonight" and carries no skip and no back — the `wnReset` "back" control at 421 belongs to the solo picks screen (`wnSoloOn`, 415–454), i.e. it is the exit from the destination, not an escape from the questions. The solo provenance line assumes the round happened: one static string, "{budget} min budget · unseen first · tilted by your three answers" (424), in which "unseen first" reports the rewatch filter and the tilt clause is concatenated, not alternative. This is residue of the round §6.2's v2.1 preamble already deletes.

**What the spec says.** §6.2 step 7: "…a **reshuffle** control; optionally a few self-administered votes to sharpen the tilt."

**Proposed amendment.** Make the entry explicit in step 7: "\"Just me\" lands **directly on the three picks and the wildcard**, ranked by the personal Ledger with no tilt. A `sharpen this` affordance runs the optional self-administered votes and re-ranks in place; the provenance line reads \"{budget} min budget · unseen first\" and gains \"· tilted by your N votes\" only once those votes exist."

**Cost.** Free copy; it deletes one state transition left over from the deleted mood round.

### 56. The lobby screen — the whole of it

**What the prototype does.** A card headed "Waiting room" plus the code in ember mono, with a dotted `leave` link (`wnLeave` resets to lobby, clearing answers and votes). Two `waitCopy` variants (2844): host → "Start whenever you are ready. Anyone who joins before you start is in."; joiner → "{Host} starts when everyone is in." Seats (2853) show avatar, name, and a mono role of **host / joined / this phone**. The host sees the CTA "Start — three questions each" (492); a non-host sees a dashed "Waiting for {host} to start" (495) plus a demo affordance. Desktop 456–498; phone 1475–1504.

**What the spec says.** §6.2 step 2 names the join channels; step 8 mentions a TV "lobby". §12 M4 scopes "lobby + open-rooms discovery". No lobby screen, seat model, role vocabulary or host/joiner copy exists in the spec.

**Proposed amendment.** Insert **step 2b — Lobby**: "The room's own screen: the room code in the data voice, a live seat list (avatar, name, and one of `host` / `joined` / `this phone`), a guest stepper (proposal 59), `leave`, and a host-only **Start** control. Waiting copy: host — \"Start whenever you are ready. Anyone who joins before you start is in.\"; joiner — \"{Host} starts when everyone is in.\" Seats update over the WebSocket; a member who leaves is removed from the seat list, not from a started round."

**Cost.** Free copy for a screen M4 must build anyway. The prototype's CTA text needs its "three questions each" replaced per the ~10-vote decision.

### 57. Session controls: where they live, who owns them, when they lock

**What the prototype does.** One control row — Film|Series pills, the runtime slider, the "exclude seen" toggle — sits **above both doors** (367–379; phone card 1420–1436 as labelled TYPE / TIME / EXCLUDE SEEN), so it configures solo and group identically. Once a room opens, no screen re-exposes them. `wnJoinRoom` (2300) overwrites the joiner's local kind/budget/ignoreSeen with the host's values.

**What the spec says.** §6.2 step 1: "Session controls: kind (film/series), a **runtime budget slider** (soft…), and a **rewatch toggle**." Silent on placement, ownership and mutability.

**Proposed amendment.** Extend step 1: "The controls sit before the solo/group fork and apply to both. Once a session exists they are **host-owned** — joiners inherit the host's kind, budget and rewatch setting — and they lock when the vote round starts. The slider runs 60–200 minutes in steps of 5, default 130."

**Cost.** Free copy; pins three constants and one ownership rule.

### 58. The winner card is the one screen with no prototype

**What the prototype does.** The result screen (602–623; phone 1598–1614) shows: a mono eyebrow "VOTES REVEALED TOGETHER", a 270px ember-bordered winner poster with the title at 30px and `{year} · {runtime} · {terms}`, a conditional green "Unanimous." (2905), and two buttons. It shows **no approval share** (the count exists only in the log line at 2418), **no per-person match lines**, **no runners-up**, **no wildcard**, and **no budget-fit line** — even though the deleted finalists screen computed all of them (2624–2635) and the solo screen renders a fit line.

**What the spec says.** §6.2 step 6: "winner card — approval share, per-person match lines in DNA terms including the honest negative (\"nothing here is their pull — *bleak* works against them\"), and **Play on Jellyfin** as the primary CTA — plus runners-up and one **wildcard**."

**Proposed amendment.** Expand step 6 into a layout: "The winner card, in order: the reveal beat (proposal 60); the winner poster with `{year} · {runtime} · {three DNA terms}` as its why-line; the approval share in the data voice (\"3 of 4 approved\"), and \"Unanimous.\" when it is; one **per-person match line** per participant, in DNA terms, including the honest negative — a guest with no grid profile reads \"{name} — no profile yet\", never nothing; the budget-fit line (\"fits your 130 min\" / \"runs 21 min over\"); **Play on Jellyfin** as the primary CTA; then runners-up as a compact row and one wildcard strip captioned \"a step outside your usual, honestly labelled\"."

**Cost.** Free copy for work M4 owns regardless — but today M4 has no design at all for its own terminal screen. This is the single largest Tonight gap.

### 59. Guest seats: cap, placement, and what ranks a profile-less guest

**What the prototype does.** A "guests" row with − / count / + (484–489); `wnGuests` clamps to 0–6 (2339). Guests become `g1…gN` appended **after** the joined members (2350–2354), named "Guest N", coloured `#6b6b72`, seat role "this phone". They are excluded from the candidate-pool average (2358) and from D (3005) — but their questions are ranked using **Patrick's ledger**: `const u = guest ? 'p' : who` (2366).

**What the spec says.** §6.2 step 1: "picks participants: members and/or N guests"; step 2: "**Guests use the initiator's phone after the initiator finishes**"; step 3: "Guests contribute no taste term unless they have a grid profile."

**Proposed amendment.** Add to step 1: "Guest count is set in the lobby, capped at 6, and guests always take their turns last on the initiator's phone." Add to step 3: "A guest with no grid profile is ranked by the **candidate pool's own order** (member average), never by a borrowed member's Ledger — the prototype silently substitutes the host's, which is not 'no taste term'."

**Cost.** Free copy; the substitution rule is a one-line fix that otherwise ships as a privacy-shaped bug.

### 60. Restore the reveal beat

**What the prototype does.** The result opens with a mono letter-spaced eyebrow **"VOTES REVEALED TOGETHER"** (604) and a `fadeIn .25s` (603), then the winner; if every participant approved, a green "Unanimous." (615–616).

**What the spec says.** §6.2 step 6 says only "**Result:** winner card". Step 4's blindness is described as a data property ("preserved by construction"), not as a moment.

**Proposed amendment.** Add to step 6: "The reveal is simultaneous on every device and opens with an explicit beat — \"VOTES REVEALED TOGETHER\" — before the winner appears; unanimity is called out when it occurs. The blind round's social payoff is the simultaneity; shipping the property without the moment ships half of it."

**Cost.** Free copy; recovers the one piece of ceremony the v2.1 rewrite dropped along with the approval round.

### 61. Nothing captures the session outcome

**What the prototype does.** The winner lives only in `wn.winner` (2417); the approval count appears only in the transient log line (2418); `wnReset` (2427) discards both. There is no post-watch prompt, no persisted approval share, and no link from the result into the §6.1 rating queue.

**What the spec says.** §4.2: `session_result(...)` and `session_outcome(session_id, chosen_title_id, approval_share, participants) -- feeds §13`. §13 targets "winner approval share … satisfaction spread < 0.3". §14 risk 6: "log every vote; compare winner satisfaction against the solo baseline."

**Proposed amendment.** Add **step 6b — Close the loop**: "On session end the app persists `session_result` and `session_outcome`. The satisfaction signal comes from §7.3's finish prompt: after a Tonight winner, \"Did you finish X?\" arrives for **every participant** and offers the verdict flow, and those verdicts are what §13's satisfaction spread is computed over. Without this path §14 risk 6's mandate to instrument the unmeasured vote round has no data."

**Cost.** Free copy; small M4 work wiring an existing §7.3 prompt to an existing table. Skipping it makes M4 unmeasurable.

### 62. Adopt the split banner and its second sentence

**What the prototype does.** The banner copy (547–549, 2899) and the clause "The axis is zeroed, not averaged — one of each is in the set below."

**What the spec says.** §6.2 step 5 quotes only the first sentence.

**Proposed amendment.** Add the second sentence to the quoted copy: it is the honest data-voice explanation the §6.8 register asks for, and the only place the zeroing rule is stated to the people it affects.

**Cost.** Cosmetic; free.

### 63. Recover D's definition from `DNA_MODEL`, and reconcile it with Taste's Δ

**What the prototype does.** `spread(t, parts)` (3004–3008) = mean(member scores) − min(member scores), guests excluded, 0 below two members. Per finalist, `dFlag = spread >= .20` renders "D = 0.24 — one of you is likely to land below your usual tonight" (2627–2629, rendered 562–564). A session-level roll-up (`anyDiverge`/`divergeCopy`, 2907) is computed and never rendered. **A second surface computes the same concept differently**: Taste → Divisive sorts by `|cdf_a − cdf_b|` and labels it `Δ` (2735–2742). For two people these are not the same number — mean-minus-min is exactly **half** the absolute gap, so §6.2's `D ≥ 0.20` fires on a couple at a 0.40 Δ, and the Divisive tab's `Δ 0.24` and Tonight's `D 0.24` describe two different amounts of disagreement.

**What the spec says.** §6.2 step 5: "Ledger divergence **D ≥ 0.20** (~14.5% of nights; below that, decide silently)" and the sanctioned copy. §6.5: "**Divisive-title list:** where the two Ledgers disagree most" — no formula, no symbol. Neither section defines its quantity, and nothing says whether they are the same one.

**Proposed amendment.** Add: "D is computed **per candidate**, not per night — the ~14.5% figure is the share of nights in which the winning candidate crosses the threshold. Guests without a grid profile are excluded from D. D's formula is **recovered from `DNA_MODEL` §5.3**, which produced both the 0.20 threshold and the ~14.5% figure, and restated in §6.2 step 5 alongside them; **§6.5's Δ is the same quantity under the same name and formula**, quoted identically in both sections." This is a recovery, not a preference: the threshold and the frequency are measured figures, so the formula that produced them is a fact in the corpus project, and inviting a choice here risks exactly the failure the entry warns about — a threshold calibrated on one formula shipped against another, off by a factor of two for a couple. Escalate to the owner only if `DNA_MODEL` turns out not to fix the formula.

**Cost.** Free copy, but the recovery must happen before M4 writes the conflict-surfacing rule and before §6.5's Divisive tab picks a sort key: two surfaces currently claim to measure the same disagreement and disagree about it.

### 64. Adopt the QR panel's join-equivalence caption

**What the prototype does.** A 250px panel (500–505): mono eyebrow "SCAN TO JOIN", a 132×132 QR placeholder, the room code at 17px mono, and the caption **"Push is best effort. The room code, the in-app banner and the TV route all reach the same session."** The panel is desktop-only; the phone lobby shows the code and no QR.

**What the spec says.** §6 preamble states the best-effort rule; §6.2 step 2 says "**Join channels, all equivalent**".

**Proposed amendment.** Adopt the caption verbatim as the lobby's normative copy, and add: "The QR appears on every viewport that has room for it; the phone lobby shows the code with a 'show QR' affordance."

**Cost.** Cosmetic; free.

### 65. Reshuffle walks the ranking; say so

**What the prototype does.** `soloShuffle` (2310) increments a seed; `soloPicks()` (2311–2318) then takes `off = (soloSeed*3) % max(1, pool.length-4)` and returns `pool.slice(off, off+3)` with `wild = pool[(off+7) % pool.length]` — a deterministic walk three places down the tilt-adjusted ranking that eventually wraps.

**What the spec says.** §6.2 step 7: "a **reshuffle** control" — semantics unspecified.

**Proposed amendment.** "Reshuffle advances down the ranked pool rather than re-drawing at random: three new picks each press, no repeats within a session, and an explicit note when it wraps. A random re-draw from a ranked list either returns the same top titles or silently degrades the picks."

**Cost.** Free copy.

### 66. Solo tilt chips and the shelf provenance line

**What the prototype does.** Meta line (424): "{budget} min budget · unseen first · tilted by your three answers", followed by tilt chips "{facet} · leaning in" / "· leaning away" / "· neutral" (425–427, 2829). Both are desktop-only; the phone solo screen (1506–1530) drops them.

**What the spec says.** §6.2 step 7 covers the picks, why-lines, fit lines, wildcard and reshuffle almost string-for-string — but not these two.

**Proposed amendment.** Add: "The solo shelf carries a provenance line — \"{budget} min budget · unseen first · tilted by your N answers\" — and, when a tilt exists, one chip per moved facet (\"{facet} · leaning in / leaning away / neutral\"). It is the solo equivalent of the group tilt reveal (proposal 67) and the §6.8 why-line for the shelf as a whole."

**Cost.** Cosmetic; free copy.

### 67. Show the group its own tilt before the winner

**What the prototype does.** The (now-deleted) finalists screen opened with a chip row over `axes` (2615–2623, rendered 542–546): "{facet} · agree +2" in green, or "{facet} · split — surfaced" in ember when `vals.length > 1 && sum === 0`. A per-axis `bar` style is computed and never rendered.

**What the spec says.** §6.2 step 5 describes combining and split-surfacing; nothing shows the group what its own answers did. The split half is covered; the **agreement** half is not.

**Proposed amendment.** Add to step 6: "The result opens by naming what the votes agreed and disagreed on, in the data voice — \"mood · agree +2 · pacing · split — surfaced\" — before it names the winner. It is the only feedback a participant gets that their ~10 votes mattered, and at M4 it doubles as §14 risk 6's instrument made visible."

**Cost.** Free copy; small M4 work. Attached to a deleted screen, so it will be lost unless written down.

### 68. Where per-person match lines go, and what a guest's says

**What the prototype does.** `topTerms` (2997–3003) sorts a title's DNA terms by the user's preference and returns the top two liked ("Patrick: bleak, propulsive"); when none are liked it returns "nothing here is their pull — {worst term} works against them". Rendered per finalist (559–561; phone 1563–1565) — i.e. **before** the vote, on the orphaned finalists card. Guests get no line at all.

**What the spec says.** §6.2 step 6 quotes the negative string verbatim and places the lines on the winner card.

**Proposed amendment.** Covered by proposal 58's layout; add only: "Match lines appear on the winner card and each runner-up, and every participant gets a line — a guest without a grid profile reads \"{name} — no profile yet\" rather than being silently omitted."

**Cost.** Cosmetic; free.

### 69. Solo picks carry Play, and a new session inherits controls

**What the prototype does.** Result CTAs: ember "Play on Jellyfin" (619; phone "Play") with **no handler**, and outlined "New session" → `wnReset` (2427), which restores every default — step, solo, host, code, joined, invited, guests 0, ignoreSeen true, budget 130, kind movie, answers, votes, winner. Each solo pick also carries its own "Play on Jellyfin" (443, 1525).

**What the spec says.** §6.2 step 6: "**Play on Jellyfin** as the primary CTA". §7.1 lists the deep link's sites as "title card, Tonight winner".

**Proposed amendment.** In §7.1, extend the parenthetical to "(title card, Tonight winner, Tonight solo picks)". In §6.2 step 6 add: "**New session** re-opens the lobby with the previous session's kind, budget and rewatch setting intact — a second round the same evening is the common case."

**Cost.** Cosmetic; free. Note for the implementer: §6.0's second title-card action does **not** exist in the prototype — the card's only action is *Show on map* (1167, 1275) — and none of the three Play controls has a handler, so the deep link's shape, its launch target and its failure state are all invention (Prototype holes 22).

### 70. Tonight's empty and failure inventory

**What the prototype does.** `wnShort` and `wnCombine` (2825) are exposed and referenced by **no template element**, so those steps render blank. The mood screen is wrapped in `<sc-if value="{{ hasQ }}">` (516/535): when `moodQs()` returns no pair for a participant, the screen renders **nothing** and the round cannot advance — no skip, no back, no copy. `phoneWait` is hardcoded `false` while `phoneWaitCopy` (2895–2896) computes two unused strings: "You're in room {code}. Waiting for the room to fill." and "Decided — see you on the sofa." There is no empty state for "no candidates fit this budget/kind", and `hasOpenRooms` false simply hides the section.

**What the spec says.** §6.2 defines the happy path only. §4.2's `answered_count` and step 4's "hidden until every participant has finished" imply a waiting state without specifying one.

**Proposed amendment.** Add to §6.2: "Four states beyond the happy path. (a) **Empty pool:** \"Nothing in the library fits 90 minutes tonight — widen the budget or include rewatches.\" (b) **No valid pair** for a participant: the round terminates short for that person and says so; it never deadlocks. (c) **Done, waiting:** each participant who finishes sees \"You're in room {code}. Waiting for {names}.\" and, at the end, \"Decided — see you on the sofa.\" (d) **Participant leaves mid-round:** the session continues with the remaining participants and the combine notes the drop."

**Cost.** Free copy — two of the four strings already exist in the prototype, disabled. (b) is a real M4 termination rule.

### 154. A fourth answer: "neither", which is not "either"

**What the prototype does.** Every this-or-that card offers a third control under the two posters: **"Neither pulls me tonight"** (532; phone "Neither pulls me", 1546), submitted as `NO_PULL`. `tilt()` then **discards it** in the first line of its loop body — `if (v === 'NO_PULL') return;` (2399) — so the answer is collected, logged as a `session_answer`, and contributes nothing. (The control lives in the deleted mood round; what is rescued here is the answer, not the round.)

**What the spec says.** §6.2 step 4: "each participant answers ~10 this-or-that pairs of real candidates … \"Which one tonight?\" `A` / `B` / **`either`**". §4.2's `session_answer` enum is `A | B | EITHER`. There is no value meaning *both of these are wrong for tonight*.

**Decided (owner, 2026-08-29): ship both.** The round offers four answers, and `EITHER` and `NEITHER` are opposite signals rather than two names for a shrug.

**Proposed amendment.** §4.2: `answer: A | B | EITHER | NEITHER`. §6.2 step 4: “`A` and `B` separate two candidates. **`either`** is an equality constraint that lifts both together — *both of these would do*. **`neither`** is a rejection that lowers both — *neither of these is tonight* — and is the strongest signal a participant can send about a live pool, since the pool is the candidate set the evening is chosen from. Copy: ‘Either is fine’ and ‘Neither pulls me tonight.’”

Under the adaptive round (§6.2 rewritten, 54c) `neither` earns its keep twice: it is the escape from a badly-constructed pool without abandoning the round, and it is the single most informative answer available to the selection rule, because it eliminates two candidates at once.

**Cost.** One enum value, one button, one update rule. It is a schema change, so it lands with §4.2 and before M4.

### 155. When the join window closes, and who may seat whom

**What the prototype does.** Two things step 2 does not describe. (i) The host's waiting-room copy (`waitCopy`, 2844) states a **rule**: "Start whenever you are ready. **Anyone who joins before you start is in.**" — the joiner's variant is "{Host} starts when everyone is in." (ii) The lobby renders a dashed **empty seat per absent member** (`emptySeats` / `hasEmptySeats`, 2856–2859; rendered 475–481 desktop, 1490–1496 phone) captioned "tap to join" and wired to `wnJoin` (2330–2336) — so a member is seated by a tap on whatever device is showing the lobby, including the host's. The invite machinery that would be the alternative — `roster` with the statuses `initiator · this device` / `joined` / `invited · push delivered` / `not invited` (2862–2872), `wnInvite` (2322–2329) and `activeSessions` (2873–2874) — is fully computed and **bound nowhere**.

**What the spec says.** §6.2 step 1: the initiator "picks participants: members and/or N guests". Step 2 lists the join channels — room code, QR, push, in-app banner — and calls them "all equivalent". Neither says when joining stops being possible, and step 1's participant-picking has no screen anywhere in the prototype.

**Proposed amendment.** Replace step 1's "picks participants" and extend step 2: "There is no participant-picking step and no invite list. A room is created, and **membership is whoever is seated when the host starts** — the host's lobby says so: \"Start whenever you are ready. Anyone who joins before you start is in.\" Start closes the join window; a late arrival joins the next session, not this one. A **fifth join channel** sits alongside the four: the lobby shows a dashed seat for every household member not yet in, and tapping one seats that member from the device in hand — for the person on the sofa who never opened the app."

**Cost.** Free copy; it deletes a step-1 clause with no design and promotes a mechanic that already works. The closing rule is a state-machine constraint the M4 WebSocket needs regardless.

### 156. The vote round's device model is asserted, not demonstrated

**What the prototype does.** `wn.voter` is a **single integer cursor** over `wnParticipants()` (2411–2419): one participant's approvals are sealed, the cursor advances, and the same screen re-renders for the next person. The CTA says so — **"Submit and pass on"** (601 desktop, 1595 phone). Every participant, member and guest alike, answers on one device. The room code, the QR panel and the WebSocket seat list exist as copy; nothing in the file demonstrates a second device answering.

**What the spec says.** §6.2 step 4: "each participant answers ~10 this-or-that pairs of real candidates **on their own device**". Step 2 confines the shared-device pattern to guests: "Guests use the initiator's phone after the initiator finishes."

**Proposed amendment.** Add to step 4: "Per-device is the default and pass-the-phone is the fallback, and the round states which it is running. **Per-device:** each participant's ~10 pairs arrive on their own phone; the lobby becomes a progress view — `Patrick ✓ · Jenny 6/10 · Mia 2/10` — with no answer content, preserving blindness while showing motion. **Pass-the-phone:** used for guests always, and for members with no device in the room; the CTA reads \"Submit and pass on\" and names who is next (\"pass to Jenny\"), and the screen clears between participants. A session may mix the two; the reveal (step 6) waits for every seat either way."

**Cost.** Real M4 work — the per-device path needs the session WebSocket to fan out pairs and collect sealed answers, which §12 M4 funds under "the ~10-vote round" but which no drawn screen covers. Naming the progress view also gives the TV route (step 8) its content.

---

## §6.3 — Rank

### 71. The tension badge needs an artefact

**What the prototype does.** After `onDrop` (2263–2270) the title simply appears in the dropped tier; the handler writes only `tierOverride` (plus a model-log line at 2269 asserting the refit). The only chip a poster can carry is the σ straddle badge (344–346) and, under `showModel`, a score/σ line beneath the poster (348–350). Nothing on the board renders a delta, a disagreement indicator or an explanation.

**What the spec says.** §6.3: "The model refits (incremental immediately, exact nightly); **if the model disagrees strongly, the title's badge shows the tension rather than snapping back** — the user is a data source, not a tyrant, and vice versa." The principle is normative; the artefact is undrawn.

**Proposed amendment.** "Tension badge: after the refit, a title whose assigned tier falls **outside the posterior's 80% credible interval** — the operational reading of 'disagrees strongly'; a one-level difference inside the interval is not tension — carries a badge in the quiet-reasons register: \"you put it in A · the ledger still reads B — 3 comparisons would settle it\". It replaces the straddle badge on that poster while it holds, since a straddling title is also queue-eligible (§6.3) and the two chips compete for the same corner. Tapping it opens the comparison queue (proposal 73) seeded with that title; it clears when the posterior agrees or the comparisons are answered."

**Cost.** Free copy; it fixes the threshold, the copy and the badge precedence for the mechanism that makes "drag-and-drop is data, not override" legible. M3 scope — but it cannot ship before proposal 73's queue screen exists, since that is its tap target.

### 72. Facet predicates match `facet.term`, not the bare term

**What the prototype does.** The Rank toolbar (327–332) is kind pills plus one free-text box; the predicate (2595) is `title.indexOf(rf) >= 0 || t.terms.some(x => x.indexOf(rf) >= 0)` — an OR over title substring and **bare** terms. Typing `mood.cosy` matches nothing, because `t.terms` holds `'cosy'` and only the detail card's `d.label` ever builds the qualified form. Genre, decade, runtime and seen-state filters exist on Library (2175–2176, 2578) and are not reused here.

**What the spec says.** §6.3: "**Filters:** genre, kind…, decade, runtime, seen-state, DNA facet/term predicates (\"show only `mood.cosy`\")."

**Proposed amendment.** Add: "Facet-qualified predicates match the `facet.term` label form (`mood.cosy`) as well as the bare term; a `dna_tag` row stores the term, so the qualified form must be constructed at query time. The board's filter state is independent of the Library's, except `kind`, which is shared."

**Cost.** Free copy; it names a data-shape trap that will otherwise be discovered in QA.

### 73. Add the comparison queue's screen

**What the prototype does.** `renderVals` computes the entire queue — `cqPool = ranked.filter(t => t.sigma > .09)`, `cqa`/`cqb` indexed by `cqi` (2601–2603) — and `cqAnswer` (2284–2288) is a working handler that logs a `tier_queue` duel and increments `cqi` and `duels`. **Grep of the whole 1903-line template finds zero bindings for any of them.** There is no "sharpen my ranking" button, no pair card, no A/B/tie control, no queue panel, on either viewport.

**What the spec says.** §6.3 specifies the **selection policy** in full — "**Comparison queue** (\"sharpen my ranking\"): boundary-targeted active selection (70% posterior-straddling pairs / 20% exploration / 10% uniform-random held out…)" — and §12 M3's exit criterion depends on it. No UI is described anywhere.

**Proposed amendment.** Add to §6.3: "**The queue's screen.** Entry from a \"sharpen my ranking\" control in the board header and from any straddle or tension badge (seeded with that title). The pair card reuses §6.1's Battle pattern — two posters are the buttons, with a mirrored `left | about the same | right` strip — and the decisive toggle applies, since tier-queue duels are margin-weighted (§5.2). Each pair carries its reason in the data voice; the session counter reads `N rated · M comparisons this session · +0.012 within-liked` (proposal 79). The anchoring rule of proposal 34 applies here too: no tier, score or σ on the pair before the answer."

**Cost.** **Real M3 work with no prototype to copy.** This is the largest hole in the whole review: §6.3's policy, §5.2's within-liked resolution and §13's held-out stream all depend on a screen nobody has drawn.

### 74. Give the phone lift an escape hatch

**What the prototype does.** `phonePickUp` (2271–2275) sets `s.phonePick === id ? null : id` — re-tapping the lifted poster puts it down with no tier edit. There is **no Cancel control on the banner, no tap-outside handler, no Escape path**, and the banner copy ("Moving **{title}** — tap a tier to drop it.", 1626) does not mention that re-tapping cancels.

**What the spec says.** §6.3: "**On phones:** tap a title (it lifts), tap a tier (it drops)". No cancel is specified.

**Proposed amendment.** Add: "The lift is cancellable: the 'Moving…' banner carries an explicit **Cancel**, and re-tapping the lifted title also puts it down. A modeless lift with an undiscoverable exit is the classic tap-to-move failure."

**Cost.** Free copy; one control.

### 75. Adopt the tap-to-tier affordances, not just the mechanic

**What the prototype does.** While a pick is active: an ember banner above the board (1626); **all** tier rows take an ember border `rgba(200,97,58,.4)` (2821); the picked poster gets a solid `#c8613a` border and every other poster drops to `opacity:.5` (2823). Persistent footer: "tap a poster to pick it up, tap a tier to drop · each move writes a tier_edit plus two duels" (1644).

**What the spec says.** §6.3 gives one sentence for the mechanic and nothing for its legibility.

**Proposed amendment.** Append: "While a title is lifted, every tier row is visibly armed, non-picked titles dim, and the board carries the banner \"Moving **{title}** — tap a tier to drop it.\" plus the standing footnote \"tap a poster to pick it up, tap a tier to drop · each move writes a tier_edit plus two duels\"."

**Cost.** Free copy; the three together are what make a modeless interaction readable.

### 76. Straddle badges at the ends of the scale

**What the prototype does.** `TIERS[Math.max(0, TIERS.indexOf('S') - 1)]` = `TIERS[0]` = `'S'`, so an S-tier title with σ > .13 renders the badge **"S/S"** (2599). No clamp, no suppression.

**What the spec says.** Nothing about the ends of the scale.

**Proposed amendment.** Add: "At the top and bottom of the tier set the straddle badge names the single adjacent tier that exists (S straddles down to A+; F straddles up to D) and never repeats the title's own tier."

**Cost.** Cosmetic; free.

### 77. Name the producer of `via = 'explicit'`

**What the prototype does.** Both write paths hardcode `via=drag_drop` (2269, 2282). The detail card shows tier as a **read-only** stat tile labelled "your tier" (1102–1105); there is no assign-to-tier picker anywhere.

**What the spec says.** §4.2: "via: **drag_drop | explicit**". §5.2: "Tier edits (drag-drop, **explicit picks**)".

**Proposed amendment.** Add to §6.3: "`via='explicit'` is produced by the tier picker on the title detail card (§6.0), which is a control, not a read-out. Without it the enum value ships with no producer."

**Cost.** Free copy; one control on an existing card.

### 78. Where the neighbourhood badge renders

**What the prototype does.** The tier appears as a bare letter in the row gutter (336), on shelf cards (178), and on the detail card as a stat tile (1103). Within a tier, items **are** ordered best-first (`ranked` is score-sorted before the per-tier filter, 2595–2598) but nothing displays that order or names the neighbours; the string "between" appears nowhere in the file.

**What the spec says.** §6.3: "**Badge shows tier + neighbourhood (\"A — between Heat and Prisoners\")**".

**Proposed amendment.** Append: "…rendered on the title detail card's tier tile and on the board's hover/long-press state. Intra-tier order is by ledger score and must be visible where the neighbourhood is named — a neighbourhood claim over an invisible ordering is unverifiable."

**Cost.** Free copy.

### 79. Adopt the resolution readout

**What the prototype does.** `resolution` (2811) = `'+' + (Math.min(16, st.duels*0.5)/1000).toFixed(3) + ' within-liked'` — it climbs 0.0005 per comparison and saturates at **+0.016**, encoding §5.2's measured band as a live gauge. Like `rankedCount` (2816) it is computed and bound nowhere.

**What the spec says.** §5.2: "comparisons add resolution *within* the liked class (**+0.008..+0.016 at 30 duels**, monotone, no cost to global ranking)". §13 lists "within-liked resolution as duels accrue" as an evaluation row. Neither mandates a user-facing gauge.

**Proposed amendment.** Add to §6.3: "The comparison queue carries a progress readout in the data voice — `N rated · M comparisons this session · +0.012 within-liked`, capped at the measured ceiling. It is the Rank analogue of §6.1's class-balance widget: the measurement turned into the queue's own motivation."

**Cost.** Cosmetic; free — it rides on the screen proposal 73 adds.

### 80. Rank's empty and zero states

**What the prototype does.** `rows` is always seven entries (2596); when `ranked` is empty the board renders seven bare 64px strips and says nothing. Library, by contrast, computes `libEmpty` (2775) and renders a message. There is also no state for a new user with too few observations to tier.

**What the spec says.** Nothing.

**Proposed amendment.** Add: "Two states. **No match:** the filter matched nothing — say so, with the active filters listed and a clear control. **Not yet tiered:** a user with too few observations sees \"Tiers appear once you've rated about 30 titles — you're at 12\" and a route into §6.1. This is the M2→M3 handoff and is otherwise unspecified."

**Cost.** Free copy; two small states.

### 81. Give the board a heading and a why-line

**What the prototype does.** The desktop board starts straight at the kind pills (327) — no heading, no title count, no comparison count, no tier legend. The phone version **does** have a "Rank" heading (1620). `rankedCount` ("N rated · M comparisons this session", 2816) exists unbound.

**What the spec says.** §6.8: "every shelf, recommendation, question and conflict carries a one-line why".

**Proposed amendment.** Add: "The board carries a heading and one line of provenance in the data voice — `N rated · learned cutpoints, refit nightly` — so seven letters are not presented as given."

**Cost.** Cosmetic; free.

### 82. Board renders best-first, and empty tiers stay

**What the prototype does.** `TIERS = ['S','A+','A','B','C','D','F']` (2057) rendered top-down (2596): a 52px mono gutter (336) and a wrapping tray with `min-height:64px` (337). Empty tiers still render as an empty strip and remain valid drop targets.

**What the spec says.** §6.3 lists the tiers ascending — "**F, D, C, B, A, A+, S**".

**Proposed amendment.** Append: "(the board renders best-first, S at the top; empty tiers stay on screen as valid drop targets)".

**Cost.** Cosmetic; free.

### 83. Describe the filter control, not only the field list

**What the prototype does.** One input (331) with the placeholder `filter — title or DNA term, e.g. cosy`, filtering live on each keystroke.

**What the spec says.** §6.3 lists six filter dimensions and no control shape.

**Proposed amendment.** Add: "The filter is one combined box matching title or DNA term (placeholder: \"filter — title or DNA term, e.g. cosy\") with the structured facets — genre, decade, runtime, seen-state — as secondary controls behind it, not six dropdowns."

**Cost.** Cosmetic; free.

### 157. Two σ thresholds, one "queue-eligible"

**What the prototype does.** The board's straddle badge fires at `t.sigma > .13` (2599) — the poster then shows `{tier}/{tier above}`. The comparison queue draws from a different population: `cqPool = ranked.filter(t => t.sigma > .09)` (2601). The two sets are not the same, and nothing on either surface says so; a title at σ 0.11 is queue-eligible and wears no badge.

**What the spec says.** §6.3: "Badge shows tier + neighbourhood…; **a straddling title shows \"A/S\" and becomes queue-eligible**" — one predicate, doing both jobs. §5.2: "Uncertainty σ per title (Laplace diagonal) drives tier badges (\"A/S straddle\") **and** the comparison queue."

**Proposed amendment.** Pin one relationship. Recommended: "The straddle badge and queue eligibility are the **same** predicate — the title's posterior crosses a cutpoint, i.e. σ exceeds the distance to the nearer boundary — so every badged title is queue-eligible and every queue-eligible title is badged; that identity is what makes the badge a usable entry point into the queue (proposal 73). Any threshold that is a bare σ constant belongs in `ledger_hyperparams.json` (§4.3), not in the UI." If instead the queue is meant to reach wider than the badge, §6.3 must name both thresholds and say why the badge is the narrower one.

**Cost.** Free copy; it removes an ambiguity that would otherwise be resolved twice, differently, by whoever builds the board and whoever builds the queue.

---

## §6.4 — Map

### 84. Rank-normalised scatter or value plot?

**What the prototype does.** `axisVal(t, facet)` (2073–2077) is the **unweighted mean** of the axis weights over only those of the title's terms that appear in the weight map, returning **0** when the title carries no term on that axis — no salience, no confidence. `spread()` (2640–2647) then sorts all titles by `axisVal`, breaks ties by `hash(title + salt)`, and assigns each a **rank percentile** `i/(n−1)`; `posOf` (2649–2653) maps that to `fx = (pct − .5) * 1.72` plus deterministic ±2.5% jitter. Net effect: the scatter is rank-equalised, always spread edge to edge, and zero-coverage titles are scattered through the middle band in hash order rather than sitting at the origin.

**What the spec says.** §6.4 fixes the axis type — "two user-selectable bipolar facet axes with **named poles**" — and the artifact that defines them: "one TSV per vocabulary-v1 facet (left pole, right pole, term → weight ∈ [−1, 1])". It also fixes the three lenses, the Show-on-map jump and determinism. It does not say how a title's position on such an axis is computed: the per-title axis score, the rank-vs-value question and the zero-coverage case are all undefined.

**Decided (owner, 2026-08-29): the value plot — explicitly provisional.** The owner has no fixed view yet (“the map is still a big open exploration point … we might iterate on the map in the future”) and asked for the most useful first answer. It is the value plot, for one reason: the map’s poles are *named*, and a rank plot makes “halfway to playful” mean “median in this library” rather than “somewhat playful” — a named pole that does not mean the pole is a lie the reader cannot detect. The spread a rank plot buys is recovered instead by scaling each axis to its **observed** distribution rather than to the theoretical [−1, 1], which uses the field without moving the zero point. **Revisit at M6 with the map in front of you** — this is a first build, not a settled question.

**Proposed amendment.** Add a **Placement** bullet to §6.4: "A title's position on an axis is plotted as a **value**, not a rank percentile: an axis on which the library genuinely clusters must look clustered, and a named pole must mean the pole." Two consequences follow that are transcription rather than choice, and are adopted with it: the per-title axis score is the **salience-weighted** mean of its terms' axis weights, salience read from the title's `dna_tag` / `dna_projected` rows and the weights from the facet's axis TSV (§4.1 rule 2 — weights, never filters; the TSV carries no salience, so the two sources are joined at read time); and a title carrying no term on that axis is drawn in a neutral "no coverage on this axis" treatment **in a gutter outside the plotted field — never at a pole, never at the origin**, since the field's edge is a named pole and a title with no evidence must not be made to assert the strongest possible claim on the axis. Such a title is a §8.4 flywheel candidate.

**Cost.** Free text, but it decides what the map *means*: rank-equalisation and value-plotting produce different pictures of the same library, and the prototype quietly chose the first. The no-coverage treatment adds one flywheel trigger.

### 85. The map canvas does not partition by kind; two lists on it do

**What the prototype does.** `mkNodes` (2658) maps over `this.titles` unconditionally — all 38 titles, 30 movies and 8 series, on one canvas. `st.kind` is never consulted on this surface and there is no kind control. The match-for-you lens then colours from `t.cdf[u]`, which `build()` computes **per kind** (2114–2120), so a series at CDF .9 and a film at CDF .9 render identically despite ranking in separate populations.

**What the spec says.** §4.1 rule 5: "every **ranking** surface partitions by it"; §5.1: "separate surfaces". The rule is scoped to ranking, so it does not reach the canvas — and the spec never says so. It does reach two things §6.4 places on the same surface: compositional-search survivors "ranked by similarity then personal score" and explore recommendations "ranked by prior + proximity". (The prototype ships the search list unranked and unfiltered at 2712–2714, so there is nothing there to copy.)

**Proposed amendment.** Add to §6.4: "The map **canvas** does not partition by kind — it is DNA geography, not a ranking, and §4.1 rule 5 does not reach it; wander follows shared-DNA edges title to title, and Show-on-map jumps from every title card, both of which a partition would break. Two things on the surface *are* rankings and do partition: compositional-search survivors and the explore-frontier list. The match-for-you lens reads a per-kind CDF and therefore renders **one colour ramp per kind**, with the legend naming both; it is never a single ramp over a mixed field."

**Cost.** Free copy; it closes a rule that currently reads as universal and is quietly exempted, and settles the two ranked lists on the surface that rule 5 already governs.

### 86. Two search failures, not one — and one of them lies

**What the prototype does.** When no vocabulary term binds, `exPredicate` (2935) renders "— could not bind any vocabulary term". But `exNoHits = st.exRan && exHits.length === 0` (2936) is **also** true in that case, so the panel simultaneously shows the empty-predicate body (650): "No owned title carries that combination. **The query went to the extraction queue** — this is how the vocabulary grows where it is actually needed." That is false: `exRun` writes the flywheel only when `terms.length && !hits.length` (2521), so an unparseable query queues nothing while telling the user it did.

**What the spec says.** §6.4 covers only the bound-but-empty case: "Empty predicates land in the flywheel with their reason (\"no owned title carries robots + gladiatorial\")."

**Proposed amendment.** Add: "Two distinct failures. **Parsed, no survivors** → the flywheel, with its reason, and the copy above. **Nothing bound** → a different message that offers a repair (nearest in-vocabulary terms; a route into the 11 facets) and **queues nothing** — a UI that reports a queue write which did not happen is worse than one that reports nothing."

**Cost.** Free copy; it corrects a shipped falsehood.

### 87. The map has no explore-frontier rendering

**What the prototype does.** The strings "frontier" and "adjacent" appear **nowhere** in the file. The idea exists only as a Home shelf (`homeRows` key `cold`, 2551–2553): "You have never watched anything {coldTerm}" / "unvisited region of DNA space next to what you like".

**What the spec says.** §6.4: "**Explore recommendations:** the *adjacent possible* — regions of DNA space near the user's liked regions but unvisited; policy from the measured explore analysis (~1 exploratory slot in 6, ranked by prior + proximity; cost ≈ −1 pp top-hit rate, honestly labelled)."

**Proposed amendment.** Add: "On the Map, the adjacent possible renders as a **frontier lens**: regions adjacent to the user's liked regions with no seen title are shaded, and the unvisited titles inside them are badged. The ~1-in-6 policy governs ranking surfaces; the map's job is to show *where* the frontier is, and the badge carries the honest cost label."

**Cost.** Free copy; M6 work with no prototype. Without it "Explore recommendations" has one shipped form (the Home shelf) and one unbuildable one.

### 88. Facet legend as an isolate filter: dim, never remove

**What the prototype does.** An 11-chip legend under the canvas (738–744, 2923–2927) built from `FACET_COLOR`. `exFacetPick` (2476) toggles the facet and clears on re-click. When a facet is isolated, nodes whose `domFacet` differs drop to `opacity .16` (or `.28` with a title also selected) — **nothing is removed**. The affordance is taught by the caption (745): "drag to pan · scroll to zoom · click a film to see its named connections · click a facet to isolate it".

**What the spec says.** §6.8 gives the palette ("A fixed colour per vocabulary facet (11)"); §6.4 gives the lens. Neither describes the legend or its semantics.

**Proposed amendment.** Add: "The facet legend is a control: tapping a facet isolates it by **dimming** every other node, never by removing them, and tapping it again clears. Selecting a title dims non-neighbours the same way. The map's sense of place depends on the context staying visible; a filter would destroy it. The affordance line — \"drag to pan · scroll to zoom · click a film to see its named connections · click a facet to isolate it\" — ships as the canvas caption."

**Cost.** Cosmetic; free copy.

### 89. How a title gets its one facet colour

**What the prototype does.** `domFacet(t)` (2078–2081) counts how many of a title's terms fall in each facet and returns `Object.keys(c).sort((a,b) => c[b]-c[a])[0] || 'mood'` — the modal facet, ties broken by **JS key-insertion order**, with a hard fallback to `mood` for a title with no mapped terms. That one value drives both the colour lens and the isolate test (2664–2665).

**What the spec says.** Nothing.

**Proposed amendment.** Add: "A title's facet colour is its **salience-weighted** dominant facet, with an explicit deterministic tie-break (vocabulary order). A title with no vocabulary coverage renders in a neutral 'unnamed' grey — never a silent default to `mood`, which makes the map lie about coverage."

**Cost.** Cosmetic; free.

### 90. Label visibility keeps the map legible

**What the prototype does.** `showLabel = selected || isNeighbour || exZoom >= 1.6` (2674) — at rest only the selection and its neighbours are named; past 160% zoom, everything is. Label size is `10.5 / max(1, zoom)` px so labels hold constant screen size. `spreadLabels()` (2678–2699) runs a greedy top-down de-overlap pass in 21px steps, capped at 14 iterations; the phone truncates neighbour labels to 13 characters.

**What the spec says.** Nothing.

**Proposed amendment.** Add: "Labels: the selection and its neighbours are always named; all titles are named past a zoom threshold; labels hold constant screen size as the field scales, and colliding labels are displaced rather than dropped. (The collision solver itself is implementation.)"

**Cost.** Cosmetic; free — but it is what keeps a 800-node map from becoming a wall of text.

### 91. Ship worked example queries, and Enter-to-submit

**What the prototype does.** A full-width input with placeholder "Gladiator but with robots", a Search button, and three example chips: "cosy period", "dread + procedural", "robots" (634–636). `exExample` (2524) writes the chip's value into `exQ` and sets `exRan:false`, so a chip **clears** the previous result and needs a second click on Search. There is no Enter submit. Two of the three shipped examples return zero survivors against the demo library — deliberately: `themes.robots` is a real vocabulary term no owned film carries.

**What the spec says.** §6.4 gives one flagship query.

**Proposed amendment.** Add: "The search box submits on Enter and ships three worked examples as chips, one of which is expected to fail — the empty-predicate path is part of what the interface teaches (§8.4). Tapping a chip runs the query; it does not merely fill the box."

**Cost.** Cosmetic; free copy.

### 92. Adopt the projected-tier section header

**What the prototype does.** `exExtracted`/`exProjected` split the hits (2713–2714); extracted chips take a green-tinted border, projected chips a dashed border at 60% opacity under the mono header **"PROJECTED TIER — INFERRED, NOT QUOTE-VERIFIED"** (653), rendered only when projected results exist. Every chip is clickable into the map (`exPick`).

**What the spec says.** §6.4: "extracted-tier results first, projected-tier in a labelled second section (two-tier rule)."

**Proposed amendment.** Adopt the header verbatim as the normative label, and add: "Results are tappable into the map, which is what closes the search→wander loop."

**Cost.** Cosmetic; free. It is the clearest statement of §4.1 rule 1 anywhere in the UI.

### 93. Adopt the flywheel's user-facing sentence

**What the prototype does.** On zero survivors (650): "No owned title carries that combination. The query went to the extraction queue — this is how the vocabulary grows where it is actually needed." `exRun` appends `{q, r: 'empty predicate — no owned title carries ' + terms.join(' + ')}` to the flywheel (2521).

**What the spec says.** §8.4 describes the flywheel to the *admin*; §6.4 states the enqueue rule. Neither has a sentence for the person who hit the dead end.

**Proposed amendment.** Adopt that string into §6.4 as the normative empty-predicate copy: it is the only place the flywheel is explained to a user, and it turns a dead end into a stated contribution.

**Cost.** Cosmetic; free.

### 94. Record the Show-on-map landing

**What the prototype does.** `showOnMap` (2462–2465) sets `{surface:'explore', exSel:id, sel:null, exZoom:1.6, exPan:{x:0,y:0}, exPanPct: centerPanPct(id, 1.6)}` — it closes the title card and lands on the scatter already centred on the title at 1.6× zoom.

**What the spec says.** §6.4: "a **Show on map** jump from every title card."

**Proposed amendment.** Append: "…which selects the title, zooms in and centres it, rather than dropping the user at the default overview."

**Cost.** Cosmetic; free.

### 95. The map's own no-bundle and no-axis states

**What the prototype does.** The map always renders 38 nodes from the hardcoded fixture. There is no empty state, no loading state, and no branch for a missing bundle or missing axis definitions anywhere in 628–773; the only empty state on the surface is the search panel's.

**What the spec says.** §3.1 covers the bundle-level case globally. A **missing axis TSV for the selected facet** — likelier, since vocabulary v1's era and sensibility poles are "freshly authored" — is not covered anywhere.

**Proposed amendment.** Add: "With no bundle, the Map shows the §3.1 state. With a bundle whose vocabulary lacks an axis definition for a selected facet, the axis picker marks that facet unavailable and names why (\"no authored poles for `era` in vocabulary v1\") rather than plotting every title at zero."

**Cost.** Free copy; one state.

### 158. The map opens somewhere; say where, and pin the view constants

**What the prototype does.** Initial state (2150–2152) is `exLens:'facet'`, `exX:'tone'`, `exY:'setting'`, `exZoom:1`, pan zero — the map opens on a specific axis pair under the facet-colour lens. The view constants are all hardcoded: zoom clamps to **0.6–4** in both `exWheel` (1.12 per notch, 2482–2484) and `exZoomBy` (`×0.8` / `×1.25` buttons, 2477–2480); `exReset` returns to 1× centred (2481); `exMoved` (2152) flips once a drag passes **4 px** so a pan never selects a node (2490). `exAxisPick` (2467–2475) **swaps** the two axes when a facet already on one slot is picked for the other, so the pair can never be degenerate.

**What the spec says.** §6.4 lists candidate axes ("mood: heavy ↔ light · pacing: patient ↔ propulsive · …"), names three lenses, and says "with zoom/pan". No default pair, no default lens, no zoom range, no drag-versus-tap rule.

**Proposed amendment.** Add to §6.4: "The map opens on a named default pair — **x = mood (heavy ↔ light), y = pacing (patient ↔ propulsive)** — under the facet-colour lens, at 1× and centred; the pair and lens persist per user. Zoom runs 0.6×–4× with a reset to 1×, and the current level is legible. Selecting a facet already on the other axis **swaps** the two rather than plotting a facet against itself. A pointer drag past a small threshold is a pan and never a selection." (The prototype's `tone`/`setting` are not vocabulary-v1 facets — see §6.4's binding note — so the default must be named in the real 11.)

**Cost.** Free copy; four constants and one default. Unpinned, every implementation opens the map somewhere different and the surface has no canonical first impression.

### 159. Three map controls exist only on the wide layout — including zoom

**What the prototype does.** The wide map carries a control row above the canvas — `−` / zoom label / `+` / `reset` (700–704, `exZoomBy` and `exReset`) — an 11-chip facet legend that doubles as the isolate filter (738–744, `exFacetPick`) and the upper axis label `↑ {{ ayHigh }}` (708). The compact map (1647–1730) carries the search pill, both axis pickers and the three lens chips, but of those three: **no zoom controls**, **no facet legend or isolate control**, and no upper axis label — it renders `↓ ayLow`, `← axLeft` and `axRight →` (1670–1672) and stops. Worse, its only pan/zoom wiring is `onMouseDown/Move/Up` (1649): `exWheel` is bound on the desktop canvas alone (707) and there is no touch or pinch handler anywhere in the file, so on the device §6 calls primary the map cannot be zoomed at all — only the `reset` FAB (1705), which resets a zoom the user could not change.

**What the spec says.** §6.4: "with **zoom/pan**, three lenses… " — unqualified by form factor. §6 preamble: "phone-first…, desktop as progressive enhancement." §6.8's palette gives the facet colours whose legend is the only key to them.

**Proposed amendment.** Add to §6.4: "Every map control is present on both layouts. Compact: **pinch to zoom and drag to pan** with a double-tap reset, the facet legend as a horizontally scrolling strip that keeps its isolate behaviour (proposal 88), and all four pole labels — a scatter missing one pole label is unreadable in that direction. The legend is not decoration: it is the only key to the facet colours, so dropping it on the phone leaves the default lens unexplained."

**Cost.** Free copy; real M6 phone work. Pinch-zoom is the item to schedule — the prototype demonstrates no touch gesture anywhere.

---

## §6.5 — Taste

### 96. The sweet spot is ranked by plain average, not max-min

**What the prototype does.** `sweet` (2744) sorts by `Math.min(cdf[A], cdf[B])` descending and takes 8 — an egalitarian **max-min** rule — under the copy "The region you both like. Doubles as the couple's watch-now prior and as the lens for what to acquire next." (860).

**What the spec says.** §6.5: "**Shared sweet spot:** the region both like — **doubles as the couple's watch-now prior**". §6.0's shelf table says the same independently: "the shared sweet spot — doubles as the Tonight prior". §6.2 step 3 mandates the Tonight pool be "ranked by the **plain average** of member Ledger scores". §0: "no aggregation rule dominates plain averaging, and dominance rules cost **−0.012**" against a noise floor of 0.003–0.008.

**Proposed amendment.** Add to §6.5: "The **Shared sweet spot** is ranked by the **plain average** of the two Ledger scores — the same rule as the §6.2 pool, because it *is* that pool's prior (§6.0, §6.5); a dominance rule such as max-min is measured at −0.012 against a 0.003–0.008 noise floor (§0)." Three spec sentences chain to this one answer; keeping max-min would mean deleting two of them and adopting a rule §0 prices below the noise floor, in a document whose operating law is "measured beats asserted".

**Cost.** Free copy once decided; it is a one-line sort. Leaving it means two surfaces claim to show the same thing and disagree.

### 97. Taste must partition by kind

**What the prototype does.** Both `divisive` (2735) and `sweet` (2744) operate on `this.titles.slice()` with **no kind filter**, so *The Bear*, *Severance* and *Chernobyl* sit in the same ranked list as *Heat*. `cdf` is computed **per kind** (2114–2120), so a series' 0.92 and a film's 0.92 are percentiles of different populations — and both lists **order** across the two: `divisive` sorts on `|cdf[A] − cdf[B]|` (2735, 2741) and `sweet` on `min(cdf[A], cdf[B])` (2744), each ranking a series against a film on percentiles drawn from different populations. Within a single row the Δ is well-formed — both percentiles belong to the same title, hence the same kind; it is the ranking that is not. There is no Films/Series control on the surface.

**What the spec says.** §4.1 rule 5: "every ranking surface partitions by it"; §5.1: "separate surfaces". §6.5 never mentions kind.

**Proposed amendment.** Add to §6.5: "**Divisive** and **Shared sweet spot** are ranking surfaces and partition by kind like every other (§4.1 rule 5); the surface carries the Films/Series control. **Facet silhouette** and **The seven axes** are whole-profile views and do not partition. Both lists are ordered within one kind at a time, so no ranking mixes two per-kind CDF populations."

**Cost.** Free copy; one control and one predicate. Without it both lists rank two populations in one ordering.

### 98. Define "Ledger-weighted affinity"

**What the prototype does.** `facetAffinity(u,f)` (2567–2571) is the **unweighted** mean of `score(u,t)` over every title carrying at least one tag in facet `f`: `titles.forEach(t => { if (t.dna.some(d => d.facet === f)) { s += score(u,t); n++ } }); return n ? s/n : .5`. No salience weighting, no per-term weighting, no normalisation for how common the facet is. Because most titles carry a mood or tone tag, the bars cluster near 0.5 and barely separate the two people.

**What the spec says.** §6.5: "each person's **Ledger-weighted affinity**" — a phrase that appears nowhere else and is never defined. §5.2 defines the latent and its per-kind CDF, not any facet aggregation.

**Proposed amendment.** Add: "Facet affinity is the **salience-weighted** mean of the user's Ledger CDF over titles carrying that facet's terms, **centred on that user's own mean CDF** so the bars measure relative pull rather than generosity — the same offset confound that holds back the 8th axis. Without the centring the silhouette measures how generous a rater is."

**Cost.** Free copy; it is the difference between a chart that separates two people and one that does not. Every implementation will otherwise produce a different figure.

### 99. Say how the two dots are placed, and add the axis loadings to the bundle manifest

**What the prototype does.** Dot positions are `12 + hash(axisName + userId) * 74` percent (2726–2731) — **pure hash noise**, not derived from either Ledger, so the panel is decorative. The calibration pairs are hardcoded to demo-library titles (Whiplash↔Paddington 2, Under the Skin↔Past Lives, Tampopo↔Chernobyl, Dune↔The Bear, Twin Peaks: The Return↔In Bruges, Hereditary↔Gladiator, The Grand Budapest Hotel↔Severance).

**What the spec says.** §6.5: "each with its calibration film pair, **both users plotted per axis**", with the pairs sourced by pointer to CONTENT_TASTE §5.

**Proposed amendment.** Add: "A user's position on a taste axis is the projection of their Ledger-weighted DNA profile onto that axis's loading vector, rescaled to the axis's calibration pair (the named films sit at the poles). The loading vectors and the calibration pairs ship in the bundle alongside `judgement_set_v1.tsv` (§4.3) — they are authored artifacts, not app-side derivations." §6.5 already sources the seven axes and their calibration pairs by pointer to the verified axis analysis (CONTENT_TASTE §5), so the loading vectors are corpus-side deliverables to be shipped, not an app-side method to be chosen; what is missing is the manifest entry and the sentence saying so.

**Cost.** Free text, but it adds a file to the bundle manifest and therefore to the corpus-side `mdc export-bundle` deliverable (§10). Nothing in the spec today tells an implementer how to place the dots.

### 100. Adopt the eighth-axis disclosure copy

**What the prototype does.** A dashed note closes the axes tab (835): "An eighth axis — Small-and-observed ↔ Big-and-staged — is held back. It carries a generosity confound with each user's mean-affinity offset and merges into axis one under a row-centred refit. It ships after re-derivation, not before." Desktop only.

**What the spec says.** §6.5 states the hold-back and the r = −0.80 confound as a design decision, not as user-facing copy.

**Proposed amendment.** Add: "The hold-back is *shown*, not silently applied: the axes tab closes with the note above, on every viewport. A missing axis explained is quiet reasons; a missing axis unexplained is a gap."

**Cost.** Cosmetic; free.

### 101. Adopt the divergence copy rule as shipped wording

**What the prototype does.** The Divisive tab's footer (854): "Contestedness is measured against these two ledgers, not against films that divide audiences in general." and "Divergence predicts a night below your usual — never that anyone will hate it."

**What the spec says.** §6.5: "The §6.2 copy rule applies here too: divergence copy predicts a relatively worse night, never active hate."

**Proposed amendment.** Inline the second sentence as the shipped string. Note that the first sentence is a **claim the ranking must earn** — see the prototype holes list; today it is asserted over a bare Δ sort.

**Cost.** Cosmetic; free.

### 102. Record the picker's behaviours

**What the prototype does.** A `COMPARING` row with two slots and `vs` (782–804); each slot opens a 236px dropdown with a `search people` input filtering the roster (2884–2887) and rows showing swatch, name and role (`member` / `guest`). `cmpPick` (2431–2435) silently moves the **other** slot when a selection would duplicate it: `if (c[0]===c[1]) c[d.slot==='a'?1:0] = ROSTER.find(x => x !== d.v)`. Default `['p','j']` (2145). The phone repeats it without the search input (1733–1745).

**What the spec says.** §6.5: "Compares **any two profiles** — members and persistent guests — via a two-slot picker, defaulting to Patrick vs Jenny."

**Proposed amendment.** Append: "…The picker is searchable, shows each person's role, and selecting someone already in the other slot swaps that slot rather than erroring."

**Cost.** Cosmetic; free.

### 103. One name for the surface

**What the prototype does.** The surface is titled **"My Taste"** (776; phone 1734) while the tabs match §6.5's four names verbatim (2772).

**What the spec says.** §6 names the surface "Taste".

**Proposed amendment.** Use **Taste** in the nav and **"Taste — {A} vs {B}"** as the surface heading; "My Taste" misdescribes a two-profile comparison.

**Cost.** Cosmetic; free.

---

## §6.6 — Admin

### 104. The flywheel's four-step flow has no controls

**What the prototype does.** Every flywheel row is a static div with a right-aligned estimate (967–972): "empty predicate · \"Gladiator but with robots\"" → `est. $0.42 / 48 titles`; "thin sound facet · 7 post-2025 titles" → `est. $0.07`; then a live feed from Explore appended by `exRun` (2521) and rendered with a "queued just now" chip. There is **no checkbox, no batch selection, no provider picker, no launch, no total, and no confirmation against the monthly cap**. Two of §8.4's four feed categories never appear.

**What the spec says.** §8.4: "**Admin reviews the queue, picks a batch and providers, sees the cost estimate, launches.**" §6.6 Data: "extraction queue (§8.4) with **approve/spend controls**."

**Proposed amendment.** Expand §6.6's clause: "…with approve/spend controls: rows are selectable, selection shows a running total against the remaining monthly cap, providers and pass count are chosen per batch, and Launch is disabled — with its reason — when the total would exceed the cap (§8). The queue shows all four §8.4 feeds, each labelled with its reason."

**Cost.** Free copy; real M5 work with no prototype. The interactive half of the flywheel does not exist.

### 105. The two ledger editors do not exist and must not be merged

**What the prototype does.** The Data tab contains exactly three cards — Artifact bundle (936), Acquisition board (949), Extraction flywheel (965). There is **no** view of rejected DNA tags, no low-evidence review, no `adjudications_v1.tsv` editor, no `corrections_v1.tsv` editor, and no axis-pole TSV editor — despite the title card and the wizard both leaning on the extracted/projected distinction and evidence quotes.

**What the spec says.** §6.6: "review of DNA rejects and low-evidence tags — ledger editors writing the TSV formats the corpus project already uses: `adjudications_v1.tsv` for DNA verdicts *and* `corrections_v1.tsv` for credit facts". §6.4 sends the axis TSVs to "the §6.6 ledger editor". §14.5 names the 787-rows-reverted-twice scar.

**Proposed amendment.** Add to §6.6 Data: "**Three separate editors with separate semantics**, never one merged 'corrections' screen: DNA verdicts (`adjudications_v1.tsv`, applied at ingest), credit facts (`corrections_v1.tsv`, applied **last** at every derive — §8 stage 3), and the per-facet axis TSVs (§6.4). Each shows its rows, its provenance, and the derive that will re-apply it."

**Cost.** Free copy; an entire M5 sub-surface with no prototype reference. §14.5 makes the separation load-bearing.

### 106. The Jellyfin card's three missing halves

**What the prototype does.** The card (887–904) has the URL and masked key, the §7.3 warning copy, a two-row user-mapping table with static green `token ok` chips, Test / Sync now, and the flat text "webhook: ItemAdded · debounce 10 min". There is **no library-selection control anywhere in the file**, no way to add a mapping or obtain a per-user token, no re-link path, and no error state. None of the fields or buttons are wired.

**What the spec says.** §6.6: "Jellyfin (URL, API key, **library pick**, user-mapping table, test button, sync now, webhook status)". §7.3: "at user-link time obtain per-user access tokens (`POST /Users/AuthenticateByName`) … Costs: one-time password entry per linked user; **a 401 on write → re-link prompt**."

**Proposed amendment.** No change to the requirements; add one clause for the undrawn parts: "The user-mapping row is the link control: it opens the per-user token dialog (one password entry per linked user), shows token state, and surfaces the 401 re-link prompt in place. Library pick is a multi-select over `/Library/MediaFolders`, and the webhook status names the last `ItemAdded` received and whether the 15-minute delta poll is currently carrying the load."

**Cost.** Free copy; M5 work the prototype leaves entirely to invention.

### 107. The spend cap is display-only

**What the prototype does.** "monthly spend" / "$4.12 of $25.00" over a 6px ember bar at `width:16%`, captioned "≈ $0.005–0.01 per title per pass · **thinking tokens billed as output**" (924–928). There is no input to set the cap, no per-title estimate before enabling a provider, and no behaviour at the cap.

**What the spec says.** §6.6: "Spend guard: **per-title cost estimate before enabling**, **monthly cap**, running meter". §8: "paid stages (6) never auto-retry past the spend cap." §9: Gemini "bills thinking tokens as output — counting visible JSON understates cost ~5×".

**Proposed amendment.** Add: "The cap is editable in place and takes effect immediately; enabling a provider shows its per-title estimate first; at the cap, paid stages park with the reason \"over spend cap\" rather than failing silently, and the meter says so. The caption carries the thinking-token note — it is the only place §9's 5× undercount is visible to the operator."

**Cost.** Free copy; small M5 work.

### 108. Provider cards have no editing affordance

**What the prototype does.** Three static divs (906–912): Gemini `2.5-flash · batch` + green `key set`; Anthropic `forced tool-use` + `key set`; OpenAI dashed border, dimmed, `strict schema` + grey `no key`. No key input, model picker, test button or batch/sync toggle. Task assignment is three read-only chips (913–917): `extraction → gemini + anthropic`, `query parsing → gemini`, `conflict phrasing → anthropic`.

**What the spec says.** §6.6 requires "per-provider key + model pick, a per-task model assignment … **batch-vs-sync toggle**" (§9).

**Proposed amendment.** Add: "Each provider card is editable in place — key (write-only, masked after save), model pick, test, and the batch-vs-sync toggle. The dashed, dimmed **no key** card is the normative un-configured state. Extraction may hold multiple providers (parallel mode); single-model tasks hold one."

**Cost.** Free copy; M5.

### 109. Parked jobs need actions

**What the prototype does.** The parked "Flow (2024)" card (951–961) renders an amber segment and the reason "reviews gate: 2 sources, 38 words — retry window 30 days", and carries **no interactive child at all** — no retry, no skip, no abandon, no link to the raw store. Nothing anywhere states that stage 6 will not auto-retry past the cap.

**What the spec says.** §8: "Failure at any stage parks the job with a reason, **retryable from admin**; paid stages (6) never auto-retry past the spend cap."

**Proposed amendment.** Add to §6.6 Data: "Each parked job carries its admin actions — **retry stage**, **retry from stage N**, **abandon** — plus a link to the title's raw store. A stage-6 retry that would breach the spend cap is refused with that reason shown, never queued silently."

**Cost.** Free copy; M5.

### 110. The bundle "report" is a chip with nothing behind it

**What the prototype does.** A four-chip strip `validate · report · swap · active` (2958–2959) and one button cycling `['Validate bundle','Review report','Hot-swap','Active']` (2974); `bundleNext` (2530) clamps at 3 and pushes `artifact_bundle v1 activated — backend + worker restarted` on the swap→active transition. There is no report content, no diff view, no file picker, and no way back.

**What the spec says.** §10: the importer "produces a **migration report** (counts per table, validation failures, vocabulary version)" and re-import is "a planned admin event **with a diff report** — never a silent sync."

**Proposed amendment.** Add to §6.6 Data: "'Report' is a screen, not a stage label: it shows the migration report (counts per table, validation failures, vocabulary version) and, on re-import, the diff against the active bundle — including the recompute set §10 will run. The operator can go back from it; hot-swap is the only irreversible step and confirms."

**Cost.** Free copy; M0 work (the importer page is explicitly M0 scope).

### 111. Delete the Procrustes copy — the spec already won this

**What the prototype does.** Two live strings still advertise the deleted UMAP era: the bundle card at step 3 (945) — "Recomputed against the staged bundle: user fold-in vectors, per-label-count blend weights, a full Ledger MAP refit, Cold Tower re-placement of 19 app-acquired titles, **and the explore map. The Procrustes anchor chain breaks at a basis change — the first map after import will shift.**" — and the System tab's job card (2971): "Explore map rebuild · 02:31 · 3 min 12 s · Procrustes anchored".

**What the spec says.** §6.4: "Deterministic — no nightly rebuild, no Procrustes anchoring, no map shift on bundle re-import." §10: "(The v1 Map is a deterministic axis scatter and needs no rebuild — a future UMAP lens would recompute here.)"

**Proposed amendment.** None to the spec. Flagged here so the implementation drops the map from the post-import recompute copy and replaces the job card with §5.3's actual row, "Explore-frontier + taste-viz caches · nightly".

**Cost.** Free; prototype residue, not a spec change.

### 112. Generate the System board from §5.3

**What the prototype does.** A bundle line and six job cards (2966–2973), all `ok:true` so the amber branch is dead: Ledger incremental, Nightly MAP refit, Cold Tower placement, Jellyfin sync, Explore map rebuild (stale — proposal 111), pg_dump backup. **No queue-depth reading anywhere and no log viewer or link.** Four §5.3 jobs have no card: fold-in vectors + blend weights, per-title DNA projection, explore-frontier/taste-viz caches, bundle import validation + hot swap.

**What the spec says.** §6.6: "**System:** job health, **queue depth**, last syncs, backup status, **logs**." §5.3's table has nine rows.

**Proposed amendment.** Add: "The System board is generated from §5.3's job table — one card per row, each with last run, duration, budget and status — plus the queue depth of the §8 acquisition queue and the §8.4 flywheel, and a log viewer. Adding a job to §5.3 adds a card; the two never drift apart."

**Cost.** Free copy; it makes one table the source of truth.

### 113. TMDB / OMDb / Trakt have no card

**What the prototype does.** The Connectors tab holds exactly two cards, Jellyfin and LLM providers. TMDB / OMDb / Trakt appear once in the whole file, as a static wizard row "TMDB / OMDb / Trakt — skip for now" (1047).

**What the spec says.** §6.6: "TMDB / OMDb / Trakt keys with test buttons." §8 stage 2 depends on eight fetchers.

**Proposed amendment.** Add: "Three of §8 stage 2's fetchers need credentials — TMDB, OMDb, Trakt — and each gets a key field and a test button; wikidata, wikipedia, tvmaze, rt and metacritic are keyless. Stage-2 failures on the acquisition board link to this card."

**Cost.** Cosmetic; free.

### 114. The Users list is read-only

**What the prototype does.** Every roster row is non-interactive (982–991) with a detail string: `member · 2 passkeys · jf:patrick`, `member · 1 passkey · jf:jenny`, `member · 1 passkey · jf:mia`, `guest · grid profile, 11 picks`. Footer (993): "Passkeys are bound to the public origin. Changing PUBLIC_URL invalidates every registered credential." No row edits, no role change, no passkey revoke, no re-link, no delete, no PIN management.

**What the spec says.** §6.6: "**Users:** create/edit, roles, passkey management, Jellyfin links, guest profiles."

**Proposed amendment.** Add: "Each row shows role · passkey count · Jellyfin link (or, for a guest, grid-profile status) and opens an editor: role, passkey list with revoke, Jellyfin re-link, PIN reset, OTP reissue (proposal 1), and delete. The `PUBLIC_URL` warning belongs on this list as well as in the wizard, because revocation is felt here."

**Cost.** Free copy; M5 (or M1, wherever passkey management lands).

### 115. Role gating is stubbed

**What the prototype does.** `canAdmin` is the literal `true` (2750), so Admin and Setup appear for every demo user — all of whom have role `member` (2048–2053). The phone menu's Admin row (1206) is not even wrapped in the guard, and the phone has no Setup entry. Nothing models a 24-hour admin re-prompt.

**What the spec says.** §6.6 is titled "Admin view (**admin role only**)"; §3.1: member = "full product, **no admin**"; §3.2: "admin routes re-prompt after 24 h".

**Proposed amendment.** None to the requirements; add one clause to §6.6: "Admin entries are hidden, not merely disabled, for non-admin roles, and entering any admin route re-prompts for authentication if the last admin auth is older than 24 h (§3.2)."

**Cost.** Cosmetic; free. The prototype's `true` is scaffolding.

### 116. Add the missing half of the parallel-mode caption

**What the prototype does.** The toggle (918–923) defaults **on**, labelled "Parallel mode — union-merge, agreement as confidence", captioned "union recalls 93% · intersection 67%".

**What the spec says.** §6.6 specifies exactly those numbers and adds: "**agreement is a weight, never a filter**".

**Proposed amendment.** Extend the shipped caption to "union recalls 93% · intersection 67% · agreement is a weight, never a filter" so the §4.1 rule-2 constraint travels with the control that could violate it.

**Cost.** Cosmetic; free.

### 160. Parallel mode ships on by default, which is a spend decision

**What the prototype does.** `parallel: true` in the initial state (2153): the extraction toggle is **on** before an admin has touched it, and the phone Connectors card states the resulting configuration as fact — "gemini + anthropic · parallel mode on · $4.12 of $25.00 this month" (1844–1846). Nothing in the wizard sets it, so a household that never opens the toggle runs N-model extraction from first boot.

**What the spec says.** §6.6: "**parallel mode**: run extraction on N selected models and merge by the measured consensus rule … Spend guard: per-title cost estimate before enabling, monthly cap, running meter". §8.4 / §371 repeat the mode. No section states its default, and "cost estimate **before enabling**" reads as an off default that the admin turns on.

**Proposed amendment.** Add to §6.6: "Parallel mode is **off** on a fresh install — a single provider, single pass. Turning it on is a spend action: the per-title estimate and the projected monthly figure against the cap are shown first, and the flywheel's queued-cost estimates recompute at the parallel rate. The §12 M5 default and the wizard's connector step agree with this: a household that never opens the toggle never doubles its bill." If the owner wants it on by default — the recall case is 93% vs 67% — §6.6 must say so and pair it with a cap the default cannot exceed.

**Cost.** Free copy; one initial value. It is small and it is real money, and it is the only defaulted spend decision in the app.

### 161. What an admin can do from a phone

**What the prototype does.** The compact Admin surface (1804–1861) renders the same **four** tabs as the desktop (`adminTabs`, 2773) and has content blocks for only **three**: Users as a read-only list (1811–1838), Connectors as two summary cards (1839–1848), and **Data as the acquisition board alone** (1849–1860) — no bundle card, no flywheel, no cost estimates. There is no `isSys` block on the phone at all (the desktop's is 1010–1020), so the System tab is a tab that leads to a blank screen. The compact Setup surface (1863–1873) is a stub: a five-segment progress bar, the wizard's tagline, and Back / Continue with **no step content of any kind**. Both are reachable from the phone account menu (1206), which is not role-gated (proposal 115).

**What the spec says.** §6 preamble: "responsive PWA, **phone-first**". §6.6 and §3.1 describe Admin and the wizard without naming a form factor. §14 risk 4 has the wizard warning "loudly" about `PUBLIC_URL` — on a device it has no screens for.

**Proposed amendment.** Add to §6.6: "Admin is phone-capable but not phone-first: the compact layout carries the full **Users**, **Connectors** and **System** tabs and the acquisition board, because those are read-and-repair tasks an admin does away from a desk. Two things are wide-layout only and say so rather than rendering empty — the **bundle import/swap wizard** and the **ledger editors** (§6.4, §14.5), both of which are long-running, destructive-by-flip operations over multi-line artifacts. The **setup wizard** ships on both: first boot happens on whatever device the household owns, and §3.1's sequence has no step that needs a wide screen."

**Cost.** Free copy, but it is a real scoping statement: it takes two surfaces out of the compact layout deliberately instead of by omission, and puts the wizard in.

---

## §6.7 — Model log

### 117. How many "show the model" toggles, and where does the toggle live?

**What the prototype does.** `showModel` (2141) gates **four** things: the §6.7 rail's data (`hasLog: sm && st.log.length > 0`, 2765), the Rank per-poster line `{{ i.s }} {{ i.sig }}` (348–350), the Tonight finalist line `group {{ f.score }}` (565–567), and the title card's `b(t) · β · gate` block (1172–1176). **No handler anywhere sets it**, so all four are dark.

**What the spec says.** §6.7 describes only the rail: "A per-user toggle (default off) reveals an ephemeral log". §6.0 lists the title card's model line unconditionally; §6.8 says model numbers "appear in the data voice next to their name … never bare".

**Decided (owner, 2026-08-29): one global per-user toggle, in the account dropdown, default off.** “Add a global per user ‘show the model’ toggle in the user dropdown menu. This is normally turned off, mainly for debugging.”

**Proposed amendment.** Add to §6.7: “**Show the model** is a single per-user preference, default **off**, living in the account chip’s dropdown (§3.2) rather than on a settings page — it is a debugging instrument reached often and briefly, and the dropdown is the one control surface present on every screen. It governs the event rail **and** every inline numeric annotation: ledger score ± σ on tier posters, group score on Tonight cards, the selection label in the tier queue. The title card’s `b(t) · β · gate` line is **not** gated — §6.0 lists it unconditionally as the M0 transparency promise (proposal 19), and it is the one place a model number is part of the product rather than part of the debugging.”

And to §3.2’s account-chip inventory: the dropdown carries the identity line, Account & passkeys, My Taste, Admin view and Setup wizard for admins, the switch-user list, **the show-the-model toggle**, and Log out.

**Cost.** Free copy once decided. Left open, implementers build the rail and drop the annotations, or show σ to users who never asked. Proposal 19 settles the title-card half in §6.0 and defers here for the rest; this is the single place the resolution is written.

### 118. The rail has no container

**What the prototype does.** `push(kind, text)` (2157) prepends `{id, kind, text}` and slices to **14** entries, never persisted. Twenty-two call sites write data-voice lines across every surface. `log` and `hasLog` are exported from `renderVals` (2765) and **no template anywhere binds them** — there is no rail, no drawer, no toggle. Event kinds are tagged (auth / admin / verdict / state / skip / undo / duel / tier / session / mood / vote / query / bundle) and the tag is never used.

**What the spec says.** §6.7 fully specifies the instrument's content and says nothing about where it lives.

**Proposed amendment.** Add to §6.7: "The rail is a right-hand drawer on wide layouts and a bottom sheet on compact ones, opened from the §6.9 profile toggle and from a keyboard shortcut. Entries carry an event kind (verdict, state, duel, tier, session, vote, query, bundle, auth, admin, skip, undo) used for colour-coding and filtering. It holds the last **15** events — a pinned depth, not "about fifteen", so an implementer and a bug report mean the same thing — and is never persisted; it is the primary M2 debugging instrument, so it must be reachable in two taps."

**Cost.** Free copy; one component in M2.

### 119. Widen §6.7's example set

**What the prototype does.** Beyond the four lines §6.7 already quotes, the prototype writes: `skipped — no row written` (2231); `undo: {title} verdict retracted` (2236); `unseen: {title} → pair swapped, no duel row written` (2254); `user_title.state({title}) = unseen → Jellyfin Played false` (2227); `duel(A vs B) = TIE — Davidson tie term δ` / `· margin decisive (w=1.6)` (2246); `session created · {code} · visible to every household device` (2296); `{name} joined {code} hosted by {host}` (2303); `approvals sealed for participant N — hidden until all submit` (2413); `blind approval revealed · winner {title} · {n}/{N} approvals` (2418); `session cookie cleared · passkey remains registered` (2163); `user created: {name} · temporary password issued · must change at first login` (2170); `artifact_bundle v1 activated — backend + worker restarted` (2530).

**What the spec says.** §6.7 gives four examples and the rule "narrating **every** model write in one human-readable line".

**Proposed amendment.** Add to the example run: the three **negatives** — "skipped — no row written", "undo: {title} verdict retracted", "unseen: {title} → pair swapped, no duel row written" — because a log that proves what did *not* happen is what makes the rail a debugging instrument; plus the session lifecycle lines (created / joined / sealed / revealed with the approval share), which are the only trace §14 risk 6's "log every vote" mandate produces at M4.

**Cost.** Cosmetic; free copy.

### 120. Three log lines that lie or drift

**What the prototype does.** (a) `cqAnswer` (2286) pushes `tier_queue duel = {v} — boundary-targeted pair (70/20/10 policy)` **unconditionally**, ignoring `cqUniform` — so on every 10th pair the log asserts boundary-targeting while the on-screen label says held-out. (b) `onDrop` (2269) ends `→ K-level ordered logit`; the phone `phoneDrop` (2282) writes the same string **without** that suffix. (c) `wnJoin` (2335) hardcodes the room code `BQ-4417` although `wnCreate` may have picked `TR-8802` or `KN-3155` (2294).

**What the spec says.** §6.7: the log narrates "every model write in one human-readable line".

**Proposed amendment.** Add: "A log line names the actual arm and the actual entities: the tier-queue line names its selection arm (`boundary-targeted` / `exploration` / `uniform-random, held out`), because §13's guard depends on the held-out stream being identifiable; and a line is identical for pointer and touch input when the semantics are identical. Non-write interactions — pan, zoom, lens, filter — produce no lines: the rail narrates model writes only."

**Cost.** Cosmetic; free — but a log that misreports the evaluation stream defeats §13.

### 121. Make the implied `seen` write auditable

**What the prototype does.** `verdict` (2212–2224) writes `verdicts[key]` and one log line; there is **no** `user_title.state` write, no Jellyfin write, and no log line saying one happened — unlike `stateBtn`, which explicitly narrates "→ Jellyfin Played false".

**What the spec says.** §6.1: "Verdict implies `seen`." §7.3: "`seen`/`unseen` set in the app writes Jellyfin's per-user Played flag."

**Proposed amendment.** Add the combined line to §6.7's examples: "`verdict(patrick, Heat) = liked → ordered-logit arm, incremental refit 31 ms · implies seen → Jellyfin Played true`". An implied write that leaves no trace is the one kind the rail exists to catch.

**Cost.** Cosmetic; free.

---

## §6.8 — Design language

### 122. Three hexes are not a palette

**What the prototype does.** Seven surface levels: ground `#0d0d0f`, chrome `#0f0f11` (header, rail, tab bar, inset inputs), cards `#141416`, popovers `#17171a`, detail panel `#111113`, phone-column ground `#0a0a0c`, phone bezel `#1a1a1e`. Text `#ece9e4` on a fixed alpha ladder (.85 / .75 / .72 / .6 / .5 / .45 / .4 / .38 / .35 / .32 / .3 / .28 / .25). Accent `#c8613a` with `#e0865e` for links and secondary CTAs, `#d2795a` for Log out, ink-on-accent `#150c08` (2205–2206), and accent tints at .08–.55. Positive `#7ea36b` (+`#a9c795` for extracted-DNA text); caution `#b79a4e` (+`#dcc98f`), used for the Jellyfin key warning (893) and the failed acquisition stage (3013). Borders: white at .07–.30.

**What the spec says.** §6.8: "ground `#0d0d0f`, cards `#141416`, one ember accent `#c8613a`".

**Proposed amendment.** Replace that clause with a token table: seven surface levels, the text alpha ladder, the accent triad (`#c8613a` base / `#e0865e` hover-and-link / `#150c08` ink-on-accent), danger `#d2795a`, positive `#7ea36b`, caution `#b79a4e`, and the border alpha ladder. Name them as tokens so Rate, Tonight and Admin do not each invent their own green.

**Cost.** Free documentation; §6.8 is already "from the prototype, normative", so the values are binding either way — writing them down is what stops drift.

### 123. Identity colours must not collide with the accent or the facets

**What the prototype does.** `USERS` (2048–2053) assigns Patrick `#c8613a` — **the ember accent itself** — Jenny `#3f7f6f` (= `FACET_COLOR.themes`), Mia `#8b6bd6` (= `pacing`), Sam `#b79a4e` (= the caution amber). Avatars are circles with `color:#0d0d0f` and a bold initial at 22px (chip), 21px (phone), 19px (switch list), 26px (wizard); cast/crew instead use a hashed `hsl(h 22% 30%)` at 44px. `FACET_COLOR.mood` is also `#c8613a`.

**What the spec says.** §6.8: "one ember accent `#c8613a` spent on **selection and primary actions**"; "A fixed colour per vocabulary facet (11)". §4.2 gives `user(..., avatar, ...)`.

**Proposed amendment.** Add: "Each user carries an identity colour used only for avatars and per-person plot marks, drawn from a palette that overlaps **neither** the ember accent (selection) **nor** the 11 facet colours (data meaning). The facet palette likewise excludes the accent — `mood` needs a colour that is not the selection colour. The avatar fallback is a coloured circle with the uppercase initial, since §4.2's `avatar` may be empty."

**Cost.** Free; three hex values to re-pick before anything ships.

### 124. Inline the facet colour table, bound to vocabulary v1

**What the prototype does.** `FACET_COLOR` (2059) is a designed set of eleven: `mood #c8613a`, `themes #3f7f6f`, `pacing #8b6bd6`, `structure #c9a227`, `visual #4d86c6`, `sound #c25f8e`, `character #5fae7a`, `dialogue #b98046`, `tone #7f7fd6`, `setting #4fa3a3`, `craft #b06a6a`. It drives the map lens (2664), the legend (2923–2927) and the Taste silhouette.

**What the spec says.** §6.8: "A fixed colour per vocabulary facet (11)." §6.4's binding note: the prototype's facet set "is not vocabulary v1 (it invents dialogue/tone/setting/craft and lacks place/era/sensibility/register)".

**Proposed amendment.** Inline the eleven values as a table (one warm, one teal, one violet, one gold, one blue, one magenta, one green, one amber, one periwinkle, one cyan, one brick — a designed set that will not be re-derived), retiring the four invented keys and assigning the four missing v1 facets from the same family. Move §6.4's binding note into the §6 preamble so it governs §6.5 and §6.8 as well as the Map.

**Cost.** Free; four colour assignments, once, before M6.

### 125. Write down the card grammar and the cold badge

**What the prototype does.** `card(t)` (3016–3027) is the shared contract at four sizes, all exactly 2:3 — 126×189 shelf, `minmax(132px,1fr)` grid, 66×99 rank chip, 88×132 phone shelf, 34×50 / 26×38 thumbnails. Overlays: seen dot + mono tier chip top-right on `rgba(13,13,15,.7)`, rank number top-left at 30% white on ranked shelves, title bottom-left over a scrim, hover `translateY(-4px)` with the border to `rgba(255,255,255,.3)`. `cold: t.n < 200` is computed and **never bound anywhere** — the §8 stage-10 badge has no drawn form in the entire file.

**What the spec says.** §6.8: "Poster-forward 2:3 cards." §8 stage 10: "appears in ranking/search/explore with a \"new — model placement, no crowd data\" badge until ratings accrue." §13 lists "cold-title badge accuracy" as a metric.

**Proposed amendment.** Add to §6.8: "Card grammar, at every size: 2:3 poster; title bottom-left over a scrim; seen dot and tier badge top-right; rank number top-left on ranked shelves; and the §8 stage-10 **cold badge** as a third top-right chip on titles with no crowd support. Poster art comes from proposal 13's sources; the hatched placeholder is the designed no-art state."

**Cost.** Free copy; it gives the cold badge one definition instead of five per-surface inventions.

### 126. Adaptive layout: name the five substitutions

**What the prototype does.** The compact rendering replaces desktop patterns deliberately: (1) left rail → bottom tab bar (1889–1895); (2) 352px right detail panel → full-screen detail with a mono `← back` row (1227); (3) hover nudge arrows → a 34px right-edge fade plus native scroll with `data-nobar` (1329); (4) map's right panel → a bottom sheet pinned `left:10px;right:10px;bottom:12px` at `rgba(19,19,22,.97)` with the search collapsed into a floating pill and the reset as a 42px FAB whose `bottom` lifts 14→118px when the sheet opens (1705, 1711, 2911); (5) Rate goes edge-to-edge — the card bleeds `margin:11px -12px -12px`, the verdict strip becomes a squared (`border-radius:2px`) 16px-padded bar at the bottom of the thumb arc with `not seen` / `skip` as dotted-underline text below (1351–1368). (Tap-to-tier is the sixth and is already §6.3.)

**What the spec says.** §6 preamble mandates phone-first; §6.3 covers tap-to-tier. The other five are undocumented.

**Proposed amendment.** Add **§6.8.1 Adaptive layout**: "Five substitutions between the wide and compact layouts: rail ↔ bottom tab bar; side detail panel ↔ full-screen detail with a back row; side panel ↔ bottom sheet (map); hover edge chevrons ↔ edge fade plus native momentum scroll; and, on action-dense surfaces, a full-bleed thumb-zone action bar with secondary actions as text beneath it. Nothing is dropped in the substitution — see §6.1's rail (proposal 43)."

**Cost.** Free copy; today phone-first has exactly one worked example and five undocumented ones sitting in the prototype.

### 127. Every hover affordance needs touch and keyboard equivalents

**What the prototype does.** `style-hover` carries real behaviour in 30 places: nav items reveal themselves only via `filter:brightness(1.25)`; account-menu rows have no affordance except a hover background; shelf chevrons are `opacity:0` until hover; cards lift on hover; the collapsed rail relies on native `title=` tooltips as its **only** labels. The phone tree (1183–1901) contains **zero** `style-hover` attributes, so tappable rows have no pressed or focus state. Nothing anywhere has a focus ring.

**What the spec says.** §6 preamble: "phone-first (48 px targets, one-handed, swipe)".

**Proposed amendment.** Add to §6.8: "Every hover affordance has a touch and a keyboard equivalent: a pressed state (the ember tint at `:active`), a visible `:focus-visible` ring, and no information conveyed by hover alone — a collapsed rail's tooltips are decoration, not labels. Node and control hit areas are ≥48 px on touch even when the drawn mark is smaller (the map's 22 px nodes carry invisible 48 px targets)."

**Cost.** Free copy; baseline hygiene that the prototype demonstrably lacks.

### 128. The pill is the selection primitive — in two sizes, not five

**What the prototype does.** `pill(on)` (2206) = `padding:8px 18px; border-radius:999px; JetBrains Mono 12px`, filled `#c8613a` with `#150c08` ink when on, hairline `rgba(255,255,255,.13)` when off; `pillSm(on)` (2205) is the same at `5px 11px / 10px`. They drive kind tabs, Tonight tabs, taste tabs, admin tabs and map lenses. **Three tab groups build their own inline strings instead** — library `kindTabs.phoneSt` (5px 12px / 10.5px, 2770) and `rateModes.st` / `.phoneSt` (10px 24px / 13px and 7px 16px / 11.5px, 2788–2790) — so five pill sizes are in circulation.

**What the spec says.** Nothing.

**Proposed amendment.** Add: "The pill is the single selection primitive, in exactly two sizes — wide 8×18 / 12 px, compact 5×11 / 10 px — filled ember when selected, hairline when not, mono label always. The prototype's three ad-hoc variants are drift to be collapsed, not copied."

**Cost.** Cosmetic; free.

### 129. Form and motion

**What the prototype does.** Radii: 999px pills and avatars, 14px bottom sheet, 12px cards and banners, 11px account menu, 9px buttons and panels, 7px nav items and inputs, 6/5/4/3px posters and badges, 2px on the phone verdict bar (deliberately squared), 38px phone frame. Motion: `width .16s ease` (rail), `background .12s`, `filter .12s`, `transform .18s ease, border-color .18s ease` (poster hover), `opacity .15s` (nudge arrows), and one keyframe `@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}}` used at .12–.25s on menus, panels and toasts. Scrollbars: webkit thumb 9px `rgba(255,255,255,.11)` radius 5px on a transparent track; `[data-nobar]` / `[data-hrow]` / `[data-noscroll]` hide them entirely. `input[type=range]` is fully restyled: 4px track `rgba(255,255,255,.12)`, 16px ember thumb with a 2.5px `#16100d` ring (14–32).

**What the spec says.** Nothing.

**Proposed amendment.** Add a "form and motion" paragraph to §6.8: the radius ladder (999 pill / 14 sheet / 12 card / 9 button / 7 control / 6 poster), a 120–180 ms ease-out standard, one `fadeIn` entrance for menus, panels and toasts, hidden scrollbars on horizontal shelves, and the ember-thumb range input.

**Cost.** Cosmetic; free — these are what make an implementation look like the prototype rather than merely use its colours.

### 130. Type conventions the spec does not state

**What the prototype does.** Google Fonts with preconnect: `Space Grotesk 400;500;700` and `JetBrains Mono 400;500` (11–13); body stack `'Space Grotesk', system-ui, sans-serif` with antialiasing. Two mono conventions are rigid: **section eyebrows** — uppercase, 9.5px, `letter-spacing:.12em`, `rgba(236,233,228,.32)` ("CAST", "DNA — EXTRACTED", "USER MAPPING", "SWITCH USER · DEMO", "VOTES REVEALED TOGETHER") — and **every** number, ID, ratio, tier letter, room code, block counter and state name in mono. Prose is never mono. `text-wrap:pretty` on every wrapping title.

**What the spec says.** §6.8: "Space Grotesk (display/body) + JetBrains Mono for every model number, ID and data annotation (the \"data voice\")."

**Proposed amendment.** Append: "Section headers are uppercase mono eyebrows (9.5 px / .12 em / 32% white). The data voice extends past numbers to tier letters, room codes, block counters and state names; prose is never mono. Shipped weights: Space Grotesk 400/500/700, JetBrains Mono 400/500 — self-hosted, because the shell cache must render offline."

**Cost.** Cosmetic; free. Self-hosting is one build step.

### 131. Shell chrome: rail, chip, and the menus that cannot be closed

**What the prototype does.** `railSt` (2758) = `width:158px` open / `58px` collapsed, `padding:12px 10px`, `border-right:1px solid rgba(255,255,255,.08)`, `background:#0f0f11`, `transition:width .16s ease`. `navStyle` (2187–2189): items `padding:9px 10px; border-radius:7px; gap:12px`, active = `1px solid rgba(200,97,58,.4)` + `background:rgba(200,97,58,.13)` + `color:#f0d9cf`; item height ≈35px. `collapseGlyph` is `«` / `»` at the rail foot (105, 2761–2762); `toggleRail` (2161) flips component state only — **nothing persists it**. The account chip (45–52, 2752–2753) is a pill with a 22px avatar, name and mono caret, going ember-tinted when open; the menu is a 232px popover at `top:40px;right:0`. **`grep` for `addEventListener|keydown|Escape|document\.` returns one unrelated hit in the whole file** — there is no outside-click or Escape dismissal for the chip menu, the map sheet, or anything else.

**What the spec says.** §6.8 sets the accent; §3.2 mentions the chip only as a profile switcher. No nav or dismissal rule exists.

**Proposed amendment.** Add a shell-chrome paragraph to §6.8: "Wide layouts carry a left rail, 158 px expanded / 58 px collapsed, with an ember-tinted active item (1 px ember border, `#f0d9cf` label) and native tooltips when collapsed; the collapse state persists per user per device. The rail is pointer chrome and exempt from the 48 px rule, which governs the compact tab bar and all surface controls. The account chip is avatar + name + caret, ember-tinted when open. **Every popover, menu and sheet dismisses on outside click and on Escape** — the prototype has neither, and an implementation copying it ships a menu you cannot click away."

**Cost.** Cosmetic copy; the dismissal rule is baseline hygiene worth stating once rather than per component.

### 132. The seen dot has two states, and amber is now free

**What the prototype does.** `card().seenDot` (3021) still has three branches — `seen → #7ea36b`, `forgotten → #b79a4e`, else `rgba(236,233,228,.25)` — of which the amber one is unreachable (proposal 12). Amber is separately in live use for caution: the Jellyfin key warning (893) and the failed acquisition stage (3013).

**What the spec says.** §4.2 fixes the two states; §6.8 says nothing about the dot.

**Proposed amendment.** Add: "The seen-state dot is 7 px: `#7ea36b` seen, `rgba(236,233,228,.25)` unseen — two states only. The amber `#b79a4e` freed by dropping `forgotten` is reserved for caution and warning states."

**Cost.** Cosmetic; free.

### 133. The bare-number exception

**What the prototype does.** Under each Rank poster, `{{ i.s }} {{ i.sig }}` renders as `0.612 ±0.07` in mono 8.5px at 35% (348–350) — a model number with **no adjacent name**.

**What the spec says.** §6.8: "model numbers appear in the data voice next to their name … **never bare**."

**Proposed amendment.** Add the exception rather than leaving it violated: "In dense grids the data voice may appear without its label where a header, legend or the model-log toggle supplies the name; everywhere else, never bare."

**Cost.** Cosmetic; free.

### 134. The phone column is a design device, not a surface

**What the prototype does.** Gated by the canvas prop `showPhone` (2766), a 380px column (1184) centring a mono eyebrow "PWA · SAME SESSION" over a 310×640 device with a 9px `#1a1a1e` bezel and a **simulated iOS status bar** (9:41, notch pill, battery) at 1187–1190. Everything below binds the same `renderVals` as the desktop.

**What the spec says.** §6 preamble: "responsive PWA, phone-first … installable".

**Proposed amendment.** None. Recorded here for implementers: the status bar, notch and bezel are the mockup's frame, not app chrome; the app's own compact header is the wordmark plus the account chip.

**Cost.** Free; no spec change.

---

## §8 — Acquisition

### 135. The flywheel enqueues immediately and visibly

**What the prototype does.** Running a compositional search on the Map that binds vocabulary terms but returns zero owned titles appends `{q, r: 'empty predicate — no owned title carries ' + terms.join(' + ')}` to the live flywheel (2521), which renders in Admin › Data with an ember border and a mono "queued just now" chip (970–972) beside two hardcoded rows carrying cost estimates.

**What the spec says.** §8.4 describes the queue as "fed automatically" and then jumps to the admin batch flow — the enqueue's latency and visibility are unstated.

**Proposed amendment.** Add to §8.4: "The enqueue is immediate and visible: a query that empties on the Map appears in the admin queue within the same session, with its reason and a 'queued just now' marker. The flywheel is a live instrument, not a nightly digest."

**Cost.** Cosmetic; free.

### 136. The board's stage names are §8's stage names

**What the prototype does.** `stageBar(done, fail)` (3010–3014) names ten segments as tooltips — `identify · enrich · derive · reviews gate · dna pack · dna extract · verify · project · place · ready` — colouring completed green, current ember, parked amber `#b79a4e`, unreached 8% white. The three demo jobs carry detail lines derived from real §8 thresholds ("reviews gate: 2 sources, 38 words — retry window 30 days"; "pass 1 of 2 · gemini + anthropic · 24k input tokens"; "placed by Cold Tower · badged \"new — model placement, no crowd data\"").

**What the spec says.** §8 lists the ten stages; §6.6 requires a "per-title stage board from `acquisition_job`".

**Proposed amendment.** Add one clause to §6.6: "the board's segment labels are §8's stage names verbatim, in order, so a parked job's reason reads against the pipeline block." This is the tightest spec/prototype match in the whole review and is worth freezing.

**Cost.** Cosmetic; free.

### 137. Which stage-2 fetchers need credentials

Covered by proposal 113 — recorded here as an §8 cross-reference: stage 2's fetcher list should mark tmdb, omdb and trakt as credentialled and the rest as keyless, so acquisition failures point at the right admin card.

**Cost.** Cosmetic; free.

---

## §10 — Migration and the bundle

### 138. Pin the bundle's packaging and import path

**What the prototype does.** The wizard's import step (1050–1061) shows a dashed drop target: "drop **bundle-v1.tar.zst** — or point at **/data/import**", then a five-line validation checklist that is §4.1 rules 1, 4, 6, 7 and 8 verbatim with their real figures (14,181 shared pairs; the frozen source ids; 315 legitimate duplicates; `%_bak%`/`%_good` denied; 73 mojibake rows repaired individually).

**What the spec says.** §10 lists the bundle's *parts* (`content.sqlite`, `reviews.sqlite`, `artifacts/`) and its size, but names no archive format and no import path. §2's volume list omits `/data/import`.

**Proposed amendment.** Add to §10: "The bundle ships as a single `bundle-v<version>.tar.zst`; the importer accepts an upload or a path under `/data/import` (a volume alongside `/data/artifacts`). The validation screen shows the §4.1 rule checks it enforced, by rule, with counts."

**Cost.** Free text; adds one volume to §2's compose list.

### 139. The migration and diff reports are screens

Covered by proposal 110 — recorded as an §10 cross-reference: §10 already mandates both reports; §6.6 must own their rendering, or "report" ships as a chip with nothing behind it.

**Cost.** Free.

### 140. Add the axis TSVs to the bundle manifest

**What the prototype does.** `AXES_DNA` (2060–2072) is a literal in the component: 11 entries of `[leftPole, rightPole, {term: weight}]`, with as few as 3 weighted terms on some axes (`sound`, `dialogue`). No UI displays or edits them.

**What the spec says.** §6.4: axis definitions are "a shipped, authored artifact … shipped in `dna_vocab/v1/`, editable in the §6.6 ledger editor." §4.3's line for that directory reads "(vocabulary TSVs, alias map, S matrix, adjudications)" and never names the axis files; §10's `artifacts/` row likewise.

**Proposed amendment.** Add the per-facet axis TSVs explicitly to §4.3's `dna_vocab/v1/` contents and to §10's `artifacts/` row, and add a coverage note: "an axis with too few weighted terms produces a scatter dominated by tie-break noise — the exporter reports per-axis term coverage and the importer warns below a floor."

**Cost.** Free text; one line in the corpus-side `mdc export-bundle` deliverable.

### 141. Drop the map from the re-import recompute copy

Covered by proposal 111 — recorded as an §10 cross-reference: §10 already says the v1 Map needs no rebuild; the prototype's contrary copy (945) and job card (2971) are UMAP-era residue.

**Cost.** Free.

---

## §12 — Build order

### 142. Milestones that gain real work from this review

**What the spec says.** §12's table is unchanged by most of the above — but a dozen proposals add scope that is currently invisible.

**Proposed amendment.** Amend the M-rows:

- **M0** — add "profile/account page and sign-in surfaces (§6.9, proposals 2 and 4)", "bundle report screen (proposal 110)" and "the catalog's decade and seen-state controls (proposal 152)". Exit criterion unchanged.
- **M1** — add "the §7.3 finish prompt as its own path, distinct from the §6.0 banner (proposal 150)". §7.3 already calls the banner path "the whole M1 behaviour"; the prototype implements the banner and not the prompt.
- **M2** — add "Home's zero-verdict fallback (proposal 20)" and "undo as an observation stack (proposal 35)". The undo contract is a write-path decision, not a UI polish item, and must land with the Ledger.
- **M3** — add "**the comparison queue's screen** (proposal 73)" and "tension badge (proposal 71)" explicitly. Today M3's exit criterion ("stable tier lists both users endorse") depends on a screen the spec never describes.
- **M4** — add "**the winner card** (proposal 58)", "session outcome capture (proposal 61)", "the lobby screen (proposal 56)" and "**per-device vote fan-out and the blind progress view** (proposal 156)". The outcome capture is what makes §14 risk 6's mandate executable; the fan-out is the only part of "the ~10-vote round" with no drawn screen, since the prototype demonstrates pass-the-phone only.
- **M5** — add "flywheel approve/launch controls (proposal 104)" and "the three ledger editors (proposal 105)".
- **M6** — add "**touch pan/zoom on the map** (proposal 159)". No touch gesture is implemented anywhere in the prototype, so the phone map currently cannot be zoomed at all.

**Cost.** No new milestone; it moves hidden work into the open. Every item listed is already implied by a §6 requirement.

### 143. The 24-hour admin re-prompt has no milestone

**What the spec says.** §3.2: "admin routes re-prompt after 24 h." No §12 row mentions it.

**Proposed amendment.** Attach it to M1 alongside passkeys.

**Cost.** Cosmetic; free.

---

## §13 — Evaluation

### 144. A stale sentence contradicts §6.2

**What the spec says.** §13, dropped rows: "…all tied to the deleted 8-axis/fairness machinery; **the mood round is 3–5 questions by design**."

**Proposed amendment.** Replace with "…the vote round is ~10 candidate votes by design (§6.2 step 4)." The clause is a v2.0 leftover contradicting the owner decision the same document's header announces.

**Cost.** Free; internal repair.

### 145. Name the data path for satisfaction

**What the prototype does.** Nothing persists a session outcome (proposal 61).

**What the spec says.** §13 targets "winner approval share … satisfaction spread < 0.3"; §14 risk 6 requires the round be instrumented at M4 "before anyone tunes it".

**Proposed amendment.** Add to §13: "Winner approval share comes from `session_outcome`; satisfaction spread comes from the post-watch verdicts §7.3's finish prompt collects from every participant after a Tonight winner (§6.2 step 6b). Both are M4 exit conditions, not M4 nice-to-haves — an uninstrumented vote round cannot be tuned."

**Cost.** Free text; promotes proposal 61 from a UI nicety to an evaluation dependency.

### 146. Say where the held-out stream is visible

**What the prototype does.** The Rank queue labels its held-out pairs — "uniform-random (held-out 10%) — this pair is never used to tune" (2815) — but the label is computed and unbound, and the log line for those pairs actively misreports the arm (proposal 120).

**What the spec says.** §13: "the 10% uniform-random comparison stream is the *only* data used to evaluate the tier model … the guard is non-negotiable."

**Proposed amendment.** Add: "The held-out arm is identifiable end to end: stored on the `duel` row's context, named in the §6.7 log line, and — optionally — labelled in the UI. What must never happen is a held-out pair recorded or narrated as boundary-targeted."

**Cost.** Free text; one column value and one string.

---

## Appendix A — requirement coverage

### 147. Two rows to add, one name to fix

**Proposed amendment.**

- Change the Views row to read "Views: Home/Library, Rate, Tonight, Rank, **Map**, Taste, Admin" and append "| §6.0–6.6" unchanged; the surface is Map everywhere (proposal 17).
- Add a row: "Profile: passkeys, PIN, password, sessions, push, model-log toggle | §6.9 (proposals 2, 4)".
- Add a row: "Sign-in, forced first-login password change, PIN sheet | §6.9, §3.1–3.2".

**Cost.** Cosmetic; free — but the coverage table currently claims full coverage of the owner's "User management + biometric login" row while no section describes where a passkey is registered.

---

## Prototype holes

Things spec v2.1 **requires** and the prototype has no design for. Implementation must invent these; there is nothing to copy. Ordered by how much invention is needed.

1. **The comparison queue's entire screen** (§6.3, §12 M3). The selection policy, the handler and the pair data all exist in the prototype (2284–2288, 2601–2603, 2815–2816) and **zero template bindings**. No entry point, no pair card, no answer control, no counter. M3's exit criterion depends on it. → proposal 73.
2. **The Tonight winner card** (§6.2 step 6). Approval share, per-person match lines, runners-up, wildcard and budget-fit line are all specified and none is drawn; the prototype's result screen is poster + "Unanimous." + two buttons (602–623). → proposal 58.
3. **The three ledger editors** (§6.6, §6.4, §14.5). No DNA-reject review, no `adjudications_v1.tsv` editor, no `corrections_v1.tsv` editor, no axis-TSV editor anywhere in the file. → proposal 105.
4. **The flywheel's approve/batch/launch controls** (§6.6, §8.4). Cost estimates render; nothing is selectable. → proposal 104.
5. **The profile/account page and every unauthenticated surface** (§3.1, §3.2, §6.7). `Account & passkeys` is a dead menu row (58, 1204); there is no sign-in, no forced password change, no PIN entry, no passkey registration, no session list, no push control, no model-log toggle. → proposals 2, 4.
6. **The model-log rail's container** (§6.7, §12 M2). Twenty-two `push()` call sites, a 14-entry ring buffer, kind tags — and no rail, drawer, toggle or binding. → proposals 117, 118.
7. **The Rank tension badge** (§6.3). The spec's "shows the tension rather than snapping back" has no drawn artefact; `onDrop` writes an override and nothing marks it. → proposal 71.
8. **Agreement shading and divergence highlighting on Taste** (§6.5). `silhouette` computes `div`, `p` and `j` (2719–2726) and `axes7` computes `gap` (2727–2733); the template binds none of them, so two of four tabs render no number at all.
9. **The crowd-divisive baseline** (§6.5). `divisive` is a bare Δ sort (2735) under a footer asserting the opposite (854). The baseline term must be invented — and ideally shown (`Δ 0.47 · crowd Δ 0.11`) so the claim is earned.
10. **The Jellyfin library pick, per-user token flow and 401 re-link** (§6.6, §7.3). None exists. → proposal 106.
11. **Explore recommendations on the map** (§6.4). "frontier" and "adjacent" appear nowhere in the file; only the Home-shelf form was built. → proposal 87.
12. **Session outcome capture and the post-watch prompt path** (§4.2, §13, §14.6). → proposal 61.
13. **The re-ask stream** (§13). Mandated from day one; no Rate handler implements or marks it. → proposal 50.
14. **`tier_edit via='explicit'`** (§4.2, §5.2). Both write paths hardcode `drag_drop`; the enum value has no producer. → proposal 77.
15. **Between-titles drops** (§6.3). The drop target is the whole row (335); the poster carries no drop handler, so §6.3's "dropping it *between* two titles emits that edit plus two margin-less duels" has no geometry — while the log line claims those duels on every drop (2269).
16. **The cold-title badge** (§8 stage 10, §13). `cold: t.n < 200` is computed (3025) and bound nowhere. → proposal 125.
17. **The queue-reason line** (§6.1). `curWhy` is computed (2796) with the spec's exact phrasing and rendered nowhere. → proposal 39.
18. **Tier-set configuration** (§6.3, §5.2). Hardcoded array; no control anywhere, including Admin. → proposals 11, 82.
19. **The Rank filter set** (§6.3). Genre, decade, runtime, seen-state and facet-qualified predicates are specified and absent from the board. → proposal 72.
20. **The TV kiosk route** (§6.2 step 8) and **persistent guest grids** (§6.2, §12 M7). Both correctly deferred, both undrawn. The TV route needs no new design — it re-renders the lobby, progress and result states — but the guest grid has no pattern at all.
21. **The offline / degraded shell state** (§6 preamble). Service-worker shell cache is required; nothing indicates when the app is running from it. → proposal 15.
22. **Play on Jellyfin** (§6.0, §7.1). §6.0 gives the title card "two actions — **Play on Jellyfin** and **Show on map**"; the card has **one** (`showOnMap`, 1167 desktop / 1275 phone) and no Play control at all. The string appears twice — on a solo pick (443) and on the Tonight winner (619) — and both are inert `div`s with no handler. No deep-link shape, no launch target, no failure state is demonstrated anywhere, so §7.1's client deep link is invention. → proposal 69.

---

## Already covered — do not re-open

Named here so the next review does not re-file them. In each case v2.1 and the prototype agree, and the spec's text is sufficient: the `not seen` single control and its Jellyfin mapping (§6.1, §7.3, Appendix A); the class-balance widget and its warning copy (§5.2, §6.1); the learning-curve string (§6.1); the pair-selection measured null and its UI copy (§6.1, §0 row 7); the block counter (§6.1); the Davidson tie and margin weights (§5.2, §4.2); tier percentile initialisation vs learned cutpoints (§6.3 — the prototype's fixed shares are demo scaffolding the spec pre-empts); drag-and-drop as observation not override (§6.3, §5.2, §0 row 21); the straddle badge (§6.3, §5.2); tap-to-tier semantics (§6.3); the acquisition stage board (§6.6, §8); the parallel-mode caption numbers (§6.6); the spend meter's two figures (§6.6); the six System job numbers against §5.3 and §2 (§6.6); the Jellyfin key warning copy (§7.3); the webhook debounce (§7.2); the flywheel queue's existence and its `themes.robots` example (§8.4); the compositional-search predicate display and two-tier split (§6.4); the three map lenses (§6.4); the seven taste axes and their calibration-pair pointer (§6.5); the eighth-axis hold-back (§6.5); the divergence copy rule (§6.2 step 5, §6.5); the solo picks, why-lines, fit lines, wildcard and reshuffle control (§6.2 step 7); the room code / QR / open-rooms join channels (§6.2 step 2); the guest hand-the-phone rule (§6.2 step 2 — the rule stands, but see proposal 156: the prototype passes the phone for *every* participant, not only guests, so it demonstrates nothing about per-device answering); the pending-verdicts banner's existence and its route into the queue (§6.0); the person-filter-to-filmography behaviour (§6.0); the display-only platform-score schema (§6.0, §4.1 rule 3); the DNA card's extracted/projected distinction with evidence quotes (§6.0, §4.1 rule 1 — but see proposal 151 on salience and source); the wizard's five-step sequence and the bundle-less legal state (§3.1); the wizard's §4.1 validation checklist (§4.1 rules 1/4/6/7/8, §10); the iOS push constraint copy (§6 preamble); the "Spielplan" rename and the prototype's "Media Graph" wordmark (spec header).

Six prototype behaviours are **residue**, not design, and need no spec text: the `forgotten` arrays and dead branches (2056, 2227, 3021); the hardcoded `canAdmin: true` (2750); the `MG-` OTP prefix (2168), which becomes `SP-` or nothing under the Spielplan name; the never-read `wizDone: []` (2154, proposal 5); and the two **Procrustes** strings — the bundle card's "The Procrustes anchor chain breaks at a basis change" (945) and the System job "Explore map rebuild · Procrustes anchored" (2971) — which advertise the deleted UMAP era against §6.4's "no Procrustes anchoring" and §10's "needs no rebuild" (proposal 111). The last pair is the dangerous kind: unlike the others it reads as a finished feature, and an implementer copying the admin board would ship it.

---

## Decisions taken (owner, 2026-08-29)

All seven are settled. Each proposal now carries its answer inline; this is the index.

| # | Question | Decision |
|---|---|---|
| 11 | Is the tier set household-level or per-user? | **Per user.** `ledger_cutpoints` already carries it; what needed writing was the re-initialisation rule and the fact that one user changing K never touches another's. The control moves to the per-user settings page, not Admin. |
| 18 | Does a person filter suspend the kind partition? | **No — and the control changes shape.** Kind becomes two independent toggles, Films and Series, either or both active, never neither. With both on a filmography is complete and the collision disappears. §4.1 rule 5 gains a precise reading: both-on renders two headed sections on any surface that *ranks*, and may interleave on one that merely *lists*. |
| 35 | How deep does Undo reach? | **One block.** Back to the start of the current block of 15 and no further; the depth matches the counter the user is already reading, and the chip disables visibly at the boundary. |
| 54 | Which slot carries the alternative on a split axis? | **Superseded by a redesign.** The round is now adaptive: pairs selected for information gain, dynamic length, ending in a shortlist with high certainty, then a blind approval ballot. There is a shortlist stage again, so the alternative takes the third finalist slot. Written up as **§6.2 — Tonight, rewritten** below. |
| 84 | Is the map a value plot or a rank-equalised scatter? | **Value plot, explicitly provisional.** A named pole must mean the pole; a rank plot makes "halfway to playful" mean "median in this library". Spread is recovered by scaling each axis to its observed distribution. Revisit at M6 with the map in front of you. |
| 117 | One "show the model" toggle or two, and where? | **One, global per user, in the account dropdown, default off.** A debugging instrument reached often and briefly. It governs the rail and every inline annotation; the title card's model line stays ungated. |
| 154 | Is there a `NEITHER` distinct from `EITHER`? | **Both ship.** `either` lifts both candidates, `neither` lowers both. Under the adaptive round `neither` is the most informative answer available — it eliminates two candidates at once — and the escape from a badly-built pool. |

Everything else in this document is free copy, a constant to pin, or a state to name.

---

## §6.2 — Tonight, rewritten (owner decision, 2026-08-29)

Proposal 54 asked which slot carries the alternative on a split axis. The owner answered by
redesigning the round instead, which makes the original question moot and most of §6.2 steps
4–6 obsolete. This section replaces them. It is the largest change in this document and the
only one that supersedes rather than amends.

**The design, in the owner's words:** Tonight is based on the personal taste profile for
registered users plus the current mood evaluated by the pairs. The pairs are smartly selected
to maximise knowledge gain and reduce uncertainty. The count is not fixed at ten — it is
dynamic, continuing until a shortlist of titles has emerged with high certainty.

### 54a. Active pair selection does not overturn §0's measured null

**What the spec says.** §0 row 6 and §6.1 both record it: "For *profiles*, no selection rule
beats random (best +0.0013, CI spans 0); for *ranking*, boundary-targeted selection does help."
§6.1 ships that as UI copy: "For profiles no selection rule beats random — the clever ones only
pay off in the tier queue."

**Why the new round is consistent with it.** The measured null is about estimating a person's
*stable taste* — a global-ranking problem, where every pair adds a little and clever choice adds
nothing. Tonight solves a different problem: identify the best few titles **inside a pool of
tens**, tonight, for a person whose mood is not their profile. That is best-arm identification,
not ranking estimation, and it is the regime the spec already concedes selection helps in — the
§6.3 tier queue, which is boundary-targeted for exactly this reason. Nothing is overturned; a
different objective is served.

**Proposed amendment.** Add to §6.1's copy so the two rules stop looking contradictory: "Random
pairs. For profiles no selection rule beats random — the clever ones pay off where the question
is *which of these few*, not *how do you rank everything*: the tier queue (§6.3) and tonight's
round (§6.2)."

**Cost.** Free copy. Without it, §6.1 and §6.2 assert opposite things about pair selection and
an implementer has to guess which is live.

### 54b. §13's uniform-random guard now binds Tonight **[non-negotiable]**

**What the spec says.** §13: "the 10% uniform-random comparison stream is the *only* data used
to evaluate the tier model — adaptively-selected pairs inflate reliability (measured effect; the
guard is non-negotiable)." §14 risk 6: the round "must be instrumented at M4 (log every vote;
compare winner satisfaction against the solo baseline) before anyone tunes it."

**Why it changes.** v2.1's round was competitive-but-not-adaptive, so the guard was a tier-queue
concern. An adaptive round with a data-dependent stopping rule is the textbook case the guard
exists for: a round that stops when it is confident will look confident whether or not it is
right, and the stopping rule and the evaluation cannot share data.

**Proposed amendment.** Add to step 4: "**One pair in ten is drawn uniformly at random from the
candidate pool and is used for neither selection nor stopping** — it is held out, exactly as
§13's tier-queue stream is, and it is the only data admissible for evaluating whether the round
works. `session_answer` carries the same `selection` discriminator as `duel` (§4.2:
`random | adaptive | uniform_holdout`)." Extend §13's added rows with: "shortlist stability —
how often the held-out pairs agree with the adaptive shortlist; and the rate at which the cap
and the escape control fire."

**Cost.** One schema column and a selection branch. Skipping it makes M4 unmeasurable, which
§14 risk 6 already forbids.

### 54c. Step 4, replaced — the adaptive round

**Proposed amendment.** Replace §6.2 step 4 entirely:

> **4. The round (adaptive length).** Each participant answers this-or-that pairs of real
> candidates on their own device — "Which one tonight?" `A` / `B` / `either` / `neither`.
>
> *What is being estimated.* Per participant, a **tonight score** per candidate: their Ledger
> score for that title — their stable taste — plus a **mood tilt** learned from this round's
> answers. The tilt is chosen-minus-rejected DNA **centred on the candidate-pool mean**, the
> measured centring lever, unchanged from v2.0. A participant with no Ledger (a guest, a member
> with too few labels) starts from the pool prior and is carried entirely by their answers.
>
> *How a pair is chosen.* To reduce uncertainty about **which titles belong in the shortlist**,
> not to rank the pool. Among candidates whose posterior interval still straddles the shortlist
> boundary, the round picks the pair whose answer would most reduce the number of titles still
> straddling it; ties are broken toward the pair spanning the widest DNA axis, because a pair of
> near-identical titles teaches nothing about the tilt. Every tenth pair is the uniform-random
> hold-out (54b) and is chosen by none of this.
>
> *Answers.* `A` and `B` separate two candidates. `either` is an equality constraint that lifts
> both together. `neither` is a rejection that lowers both — the strongest signal a participant
> can send about a live pool, and the reason a bad pool is escapable without abandoning the
> round.
>
> *Stopping, per participant.* The round ends for a person when the shortlist boundary is
> resolved — the leading candidates' intervals separated from the rest — subject to a **hard cap
> of 20 pairs**. From the sixth pair a persistent **"just pick for us"** ends that person's round
> immediately on what is known so far. Neither the cap nor the escape is a failure state, and
> both are logged: §14 risk 6 wants to know how often they fire.
>
> *Waiting.* Participants converge at different points. Someone who finishes early sees the
> others' **progress and never their answers** — "Patrick 6/6 ✓ · Jenny 9/~12 · Mia 4/~10 ·
> waiting for 2". The blind property is preserved by construction, as before.
>
> *Guests.* A guest has no profile, so their votes are the only thing known about them. Their
> pairs are selected to learn **them** — maximising information about the guest's own tilt rather
> than about a pool the members already discriminate — which naturally makes a guest's round a
> little longer. A persistent guest with a grid profile (§6.2, end) starts from it and converges
> like a member.

**Cost.** This is the substance of M4 and it is more work than v2.1's fixed ten: a per-candidate
posterior, a selection rule, a stopping rule and a waiting state. It also replaces "partial
overlap across participants so tallies are comparable" — participants now answer different pairs
by design, so the combine step (54d) works on scores, not on tallies.

### 54d. Step 5, replaced — the shortlist, and the split-axis alternative

**Proposed amendment.** Replace §6.2 step 5:

> **5. The shortlist.** Per-participant tonight scores are averaged across participants — plain
> averaging, unchanged: no aggregation rule dominates it and dominance rules cost −0.012. The
> round produces **three finalists and a wildcard**: the top three by group score, plus one
> exploratory pick honestly labelled ("a step outside your usual" — one exploratory slot in six).
>
> A hard split — divergent answers on the leading candidates, or Ledger divergence **D ≥ 0.20**
> (~14.5% of nights; below that, decide silently) — is **surfaced with the alternative in hand**,
> never silently averaged. The contested axis is **zeroed, not averaged**, and because zeroing
> only removes an influence it cannot by itself produce an alternative: **the third finalist slot
> is reserved for the highest-scoring title on the opposite pole of the contested axis**, labelled
> as such. Copy: "You're split on {facet} — here's one of each. The axis is zeroed, not averaged."
>
> Conflict copy obeys the measured constraint (DNA_MODEL §5.3): D predicts "one of you is likely
> to land below your usual tonight" (AUC 0.610), never "someone will hate this" — a hard rule on
> the §6.6 conflict-phrasing LLM task.

**Cost.** The reservation is one construction step. It is what makes step 5's promise true; the
prototype printed the promise and shipped a plain top-3 that could land wholly on one pole.

### 54e. Step 6, replaced — the blind approval ballot

**What the prototype does.** The recovered `wnVote` screen (576–604): "{name}, tap what you'd be
happy with · Approvals stay hidden until everyone has submitted", a **multi-select** over the
three finalists and the wildcard, then `Submit and pass on`, then "VOTES REVEALED TOGETHER".

**What the spec says.** §6.2 step 6 wants a winner card carrying "approval share"; §13 targets
"winner approval share (target: the winner appears in a majority of every participant's
favourable votes)" and "satisfaction spread < 0.3". Neither v2.1 nor the pairs round produces an
approval measurement — the tallies say which of two a person preferred, never whether they would
be *happy* with a title.

**Proposed amendment.** Restore the ballot as step 6:

> **6. The ballot (blind).** Each participant taps **everything they would be happy with** among
> the three finalists and the wildcard — an approval ballot, not a ranking. Approvals stay hidden
> until every participant has submitted; then they are revealed together. The winner is the title
> with the most approvals, ties broken by group score.
>
> **Approval share** — the fraction of participants who approved the winner — is the number §13
> evaluates the whole feature on, and this ballot is the only place it exists.

Then step 7 (the result card, formerly step 6) keeps its inventory: winner, approval share,
per-person match lines in DNA terms including the honest negative, runners-up, the wildcard, and
**Play on Jellyfin** as the primary CTA.

**Cost.** One screen, recovered from the prototype rather than designed. Without it §13's headline
metric has no data path and M4 cannot be evaluated at all.

### 54f. Solo mode under the new round

**Proposed amendment.** Amend step 8 (solo): "\"Tonight, for {name}\" lands **directly on three
picks and a wildcard** ranked by the personal Ledger with no tilt — the fastest path to a film
must not be slower than browsing Home. A **sharpen this** control runs the same adaptive round
against the same pool and re-ranks in place; the provenance line then reads \"tilted by your N
answers\" instead of \"unseen first\". There is no ballot in solo mode: with one participant,
approval share is not a measurement. A **reshuffle** control walks further down the ranking."

**Cost.** Free copy; it inverts one state transition. The prototype forced the question round
before showing any pick, which the spec's "optionally a few self-administered votes" never asked
for.

### 54g. What this costs the schema

**Proposed amendment.** §4.2's session block changes:

> ```sql
> session_participant(session_id, user_id NULL, role, tilt jsonb,
>     answered_count, converged_at NULL, ended_by, joined_at)
>     -- ended_by: converged | cap | escape  — §14 risk 6 wants the rate of each
> session_answer(session_id, participant, seq, title_a, title_b, answer, latency_ms,
>     selection)
>     -- answer: A | B | EITHER | NEITHER  (NEITHER is a rejection of both, EITHER a tie)
>     -- selection: adaptive | uniform_holdout  — §13's guard; hold-out pairs are used for
>     --   neither selection nor stopping and are the only data admissible for evaluation
> session_ballot(session_id, participant, title_id, approved bool, submitted_at)
>     -- the step-6 approval ballot; approval_share in session_outcome is derived from it
> ```

**Cost.** One new table, three new columns. All M4, but the schema is decided now so the M4
write-path is built against it rather than migrated afterwards.
