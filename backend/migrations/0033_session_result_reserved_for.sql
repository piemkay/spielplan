-- 0033_session_result_reserved_for — a finalist slot reserved for a seat. Spec v2.1 §6.2 step 5;
-- decision 479 (owner instruction of 2026-09-25 after the first household user test).
--
-- §6.2 step 5 surfaces a hard split "with the alternative in hand", and the only alternative it
-- had was an axis pole — so on release data, which ships no axis artifact (decision 173), every
-- split evening was decided silently: the first household evening measured D = 5.07 on raw scores
-- and showed nothing. Decision 479 surfaces an axis-less split by PERSON instead: a finalist slot
-- is reserved for the highest tonight-scored title of a seat none of whose own top three made the
-- slate, and the reveal labels it "{name}'s pick".
--
-- ITS OWN COLUMN, NEVER `reserved`. 0021's boolean means "the other pole of the contested axis"
-- to every reader: `tonight.svelte.js` prints "the other side of the split" whenever it is set, and
-- two registered tests pin exactly that meaning. A seat's pick is a different claim with a
-- different label, and folding it into the boolean would print the axis sentence over it. The
-- participant rather than a name, because a name is presentation and changes; the reveal joins it.
--
-- NULL on every row that is not a person reservation, which is every row already stored: no
-- evening before this decision reserved a slot for anybody. ON DELETE SET NULL because a seat is
-- only ever removed with its session (the FK above it cascades), and a dangling id is not a label.
--
-- The CHECK is the one invariant the two reservations share: a finalist is reserved by at most one
-- rule, so a card can never carry both labels.
--
-- A new numbered file, as always: every earlier migration is sha256-checksummed from its first
-- apply and a mismatch is a hard startup error. 0033 is the number the 2026-09-25 wave allotted
-- this workstream; 0031 and 0032 belong to other streams of the same wave.

ALTER TABLE session_result
    ADD COLUMN reserved_for bigint REFERENCES session_participant(id) ON DELETE SET NULL;

ALTER TABLE session_result
    ADD CONSTRAINT session_result_one_reservation CHECK (NOT (reserved AND reserved_for IS NOT NULL));
