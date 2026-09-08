"""Required configuration, refused at construction. Spec v2.1 §2, §14.4; decision 181.

§2 names `PUBLIC_URL`, `SESSION_SECRET` and `SECRETS_KEY` required config, and until M4.7 the
only thing that enforced it was `docker-compose.yml`'s `${VAR:?}` — one of the several ways this
app is started. Outside compose the defaults were accepted in silence, and two of them are
dangerous rather than merely wrong: an empty `SESSION_SECRET` signed every cookie with a constant
published in this repository, and a `PUBLIC_URL` of `""` or `spielplan.example.tld` bound every
passkey registered in that window to `localhost` (§14.4's "changing PUBLIC_URL invalidates
registered passkeys", arrived at by accident).

No database: `Settings` is the whole subject. [M4.7 sec-03, spec-04, ops-10, cs-44]
"""

from __future__ import annotations

import logging

import pytest
from pydantic import Field, ValidationError

from spielplan.core.config import Settings

# What an operator following `.env.example`'s generate one-liners actually produces is 43
# characters or more; these are the shortest values that still construct.
_SECRET = "a-perfectly-good-secret-not-a-real-one"


def _fails(**overrides) -> str:
    # Every field stated, so the ambient environment (a developer's `.env`, CI's workflow-level
    # variables) cannot decide which refusal fires or add a second one to the message.
    base = {
        "public_url": "https://spielplan.example.tld",
        "session_secret": _SECRET,
        "secrets_key": None,
    }
    with pytest.raises(ValidationError) as raised:
        Settings(**{**base, **overrides})
    return str(raised.value)


# --- PUBLIC_URL: the origin every passkey is bound to (§2, §14.4) ---------------------------


def test_an_empty_public_url_is_refused_by_name():
    """`config.py`'s `rp_id` is `urlparse(...).hostname or "localhost"`, so an empty value was
    not an error — it was a silent rebinding of every credential to a host the operator never
    typed. The refusal has to name the variable, because the symptom the browser reports is
    "the relying party ID is not a registrable domain suffix", which names nothing."""
    assert "PUBLIC_URL" in _fails(public_url="")


def test_a_bare_hostname_is_refused_because_it_is_not_an_origin():
    """The exact thing an operator writes when copying a hostname rather than the template's
    URL. `urlparse("spielplan.example.tld")` has no scheme and no hostname — it parses as a
    *path* — so this fell through to `localhost` too, on an install that looked configured."""
    message = _fails(public_url="spielplan.example.tld")
    assert "PUBLIC_URL" in message
    assert "spielplan.example.tld" in message, "the refusal must show what it rejected"


def test_a_non_http_scheme_is_refused():
    """§2 puts the operator's Traefik in front of one plain-HTTP port, so the two schemes the
    app can be reached on are the two it accepts."""
    assert "PUBLIC_URL" in _fails(public_url="ftp://spielplan.example.tld")


def test_a_real_public_url_still_yields_its_rp_id():
    """The counterpart, and the reason the refusal is worth having: a well-formed origin still
    produces the host WebAuthn binds to, and `_strip_trailing_slash` still runs first so
    "…/" and "…" remain one origin (§14.4)."""
    def ok(url: str) -> Settings:
        return Settings(public_url=url, session_secret=_SECRET, secrets_key=None)

    assert ok("https://spielplan.example.tld").rp_id == "spielplan.example.tld"
    assert ok("https://spielplan.example.tld/").rp_id == "spielplan.example.tld"
    # §2's own example, and the origin e2e runs against: http is legal, a port is not a defect.
    assert ok("http://localhost:8080").rp_id == "localhost"


# --- SESSION_SECRET and SECRETS_KEY: the floor is the documented gesture (§2) ---------------


def test_a_short_session_secret_is_refused_and_names_the_generator():
    """"Rotating SESSION_SECRET invalidates sessions only" (§2) is a promise about a secret that
    signs something. A short one is a secret an operator typed, and the message has to carry the
    one-liner from `.env.example` or the operator is left guessing at a length."""
    message = _fails(session_secret="short")
    assert "SESSION_SECRET" in message
    assert "token_urlsafe" in message


def test_an_empty_session_secret_is_refused():
    """The default, and the state every host-run process was in: `core/auth`, `api/rank` and
    `api/tonight` each fell back to `"insecure-dev-secret"` — a literal in the public repo — so
    an empty value produced a fully working install whose cookies anyone could mint."""
    assert "SESSION_SECRET" in _fails(session_secret="")


def test_a_short_secrets_key_is_refused():
    """`core/secrets._kek` HKDF-expands any printable string into a valid KEK by design, so
    `SECRETS_KEY=x` produced a green install whose wrapping key is guessable — guarding a
    Jellyfin API key §14.3 calls unscoped and admin-equivalent."""
    message = _fails(secrets_key="x")
    assert "SECRETS_KEY" in message
    assert "token_urlsafe" in message


def test_an_absent_secrets_key_stays_legal():
    """§3.1 makes a half-configured boot a legal state and `require_secrets_key` is the refusal
    that belongs to it — raised where a secret is actually needed, not at construction. An empty
    string is what `.env.example` ships and what `require_secrets_key` already reads as absent,
    so only a *typed* short key is a configuration error."""
    for absent in (None, ""):
        cfg = Settings(public_url="https://s.example.tld", session_secret=_SECRET,
                       secrets_key=absent)
        with pytest.raises(RuntimeError, match="SECRETS_KEY"):
            cfg.require_secrets_key()


def test_every_problem_is_reported_at_once():
    """A half-filled `.env` is one round trip, not three restarts."""
    message = _fails(public_url="", session_secret="", secrets_key="x")
    assert "PUBLIC_URL" in message
    assert "SESSION_SECRET" in message
    assert "SECRETS_KEY" in message


# --- the one way past ----------------------------------------------------------------------


def test_the_dev_flag_lifts_the_refusals_and_says_so_loudly(monkeypatch, caplog):
    """`ops/devstub.py` and README's host-run "Developing" flow have no operator and nothing
    worth protecting, so there is an escape hatch — but it announces itself, because an install
    that reaches this line by accident must say so in the log rather than sign cookies with
    nothing. The WARNING is the reason this is a flag and not a quiet special case."""
    monkeypatch.setenv("SPIELPLAN_INSECURE_DEV", "1")
    with caplog.at_level(logging.WARNING, logger="spielplan"):
        cfg = Settings(public_url="", session_secret="", secrets_key=None)
    assert cfg.insecure_dev is True
    assert any("SPIELPLAN_INSECURE_DEV" in record.message for record in caplog.records)


def test_the_flag_is_off_unless_it_is_set(monkeypatch):
    """It must not be reachable by default, or the refusals above are decoration.

    Asked of the field's own default, which means taking away both of the sources that outrank
    it. `conftest.py:53` pins `SPIELPLAN_INSECURE_DEV=0` in the environment — deliberately, so a
    developer who has the flag exported does not get a suite that cannot fail — and pydantic
    prefers the environment to a default, so until cycle 3's finding 14 this test asserted the
    fixture's "0" and not `core/config.py`. `model_config` carries `env_file=".env"` as well,
    which resolves against the directory pytest runs from; that is why `conftest` sets the value
    rather than popping it, and why `_env_file=None` here is not belt and braces but the second
    half of the same removal. Measured: with either source standing, flipping the field's default
    to True leaves this green. [M4.7 spec-04; decision 181; cycle 3 finding 14]
    """
    monkeypatch.delenv("SPIELPLAN_INSECURE_DEV", raising=False)
    cfg = Settings(public_url="https://s.example.tld", session_secret=_SECRET, secrets_key=None,
                   _env_file=None)
    assert cfg.insecure_dev is False


class _FlagOnByDefault(Settings):
    """`core/config.py`'s field with its default flipped — the change the test above is for.

    Written as a subclass because the mutation has to be the real one: a maintainer editing that
    line ships every container with §2's refusals off, `/api/docs` and `/openapi.json` served to
    anonymous callers, and cookies signable with an empty SESSION_SECRET.
    """

    insecure_dev: bool = Field(default=True, alias="SPIELPLAN_INSECURE_DEV")


def test_the_default_guard_sees_a_flag_that_defaults_to_on(monkeypatch, tmp_path):
    """docs/TESTING.md: a guard that cannot fail reads as coverage while providing none.

    Both sources are given the mutation, because either one alone hides it: the suite's pinned
    "0", and an `.env` line saying the same — the file `Settings` reads by itself, with nobody
    passing it anything, out of whatever directory the process was started in.
    """
    base = dict(public_url="https://s.example.tld", session_secret=_SECRET, secrets_key=None)
    assert _FlagOnByDefault(**base).insecure_dev is False, (
        "the suite's own environment decided this, so the shipped form proved the fixture"
    )

    monkeypatch.delenv("SPIELPLAN_INSECURE_DEV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SPIELPLAN_INSECURE_DEV=0\n", encoding="utf-8")
    assert _FlagOnByDefault(**base, _env_file=env_file).insecure_dev is False, (
        "a `.env` line decided it too, which is why deleting the variable is not enough"
    )

    assert _FlagOnByDefault(**base, _env_file=None).insecure_dev is True, (
        "with neither source left, the field's default is what is read — and this one is on"
    )
