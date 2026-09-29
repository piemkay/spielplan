"""Required configuration, refused at construction (§2, §14.4). No database."""

from __future__ import annotations

import logging

import pytest
from pydantic import Field, ValidationError

from spielplan.core.config import Settings

# The shortest values that still construct; `.env.example`'s one-liners produce 43+ characters.
_SECRET = "a-perfectly-good-secret-not-a-real-one"


def _fails(**overrides) -> str:
    # Every field stated, so the ambient environment cannot decide which refusal fires.
    base = {
        "public_url": "https://spielplan.example.tld",
        "session_secret": _SECRET,
        "secrets_key": None,
    }
    with pytest.raises(ValidationError) as raised:
        Settings(**{**base, **overrides})
    return str(raised.value)


def test_an_empty_public_url_is_refused_by_name():
    """An empty value silently bound every passkey to `localhost`; the browser's error names nothing."""
    assert "PUBLIC_URL" in _fails(public_url="")


def test_a_bare_hostname_is_refused_because_it_is_not_an_origin():
    """`urlparse("spielplan.example.tld")` parses as a *path*, so it fell through to `localhost` too."""
    message = _fails(public_url="spielplan.example.tld")
    assert "PUBLIC_URL" in message
    assert "spielplan.example.tld" in message, "the refusal must show what it rejected"


def test_a_non_http_scheme_is_refused():
    """§2 puts Traefik in front of one plain-HTTP port, so http and https are the only schemes."""
    assert "PUBLIC_URL" in _fails(public_url="ftp://spielplan.example.tld")


def test_a_real_public_url_still_yields_its_rp_id():
    def ok(url: str) -> Settings:
        return Settings(public_url=url, session_secret=_SECRET, secrets_key=None)

    assert ok("https://spielplan.example.tld").rp_id == "spielplan.example.tld"
    assert ok("https://spielplan.example.tld/").rp_id == "spielplan.example.tld"
    # §2's own example, and the origin e2e runs against: http is legal, a port is not a defect.
    assert ok("http://localhost:8080").rp_id == "localhost"


def test_public_url_is_kept_in_the_spelling_the_browser_sends_as_its_origin():
    """py_webauthn compares the expected origin with clientData's byte for byte."""
    def spelled(url: str) -> str:
        return Settings(public_url=url, session_secret=_SECRET, secrets_key=None).public_url

    assert spelled(" HTTPS://Spielplan.Example.tld:443/ ") == "https://spielplan.example.tld"
    assert spelled("http://localhost:8080") == "http://localhost:8080"
    assert spelled("http://localhost:80") == "http://localhost"


def test_a_public_url_with_anything_after_the_origin_is_refused():
    """No browser origin carries a path, a query or credentials, so every ceremony would fail."""
    for url in ("https://spielplan.example.tld/app", "https://spielplan.example.tld?x=1",
                "https://user@spielplan.example.tld"):
        assert "PUBLIC_URL" in _fails(public_url=url), url


def test_a_short_session_secret_is_refused_and_names_the_generator():
    """The message must carry `.env.example`'s one-liner, or the operator guesses at a length."""
    message = _fails(session_secret="short")
    assert "SESSION_SECRET" in message
    assert "token_urlsafe" in message


def test_an_empty_session_secret_is_refused():
    assert "SESSION_SECRET" in _fails(session_secret="")


def test_a_short_secrets_key_is_refused():
    """`core/secrets._kek` HKDF-expands any printable string into a valid KEK, so `x` would be guessable."""
    message = _fails(secrets_key="x")
    assert "SECRETS_KEY" in message
    assert "token_urlsafe" in message


def test_an_absent_secrets_key_stays_legal():
    """§3.1: a half-configured boot is legal; only a *typed* short key is a configuration error."""
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


def test_the_dev_flag_lifts_the_refusals_and_says_so_loudly(monkeypatch, caplog):
    """The WARNING is the reason this is a flag and not a quiet special case."""
    monkeypatch.setenv("SPIELPLAN_INSECURE_DEV", "1")
    with caplog.at_level(logging.WARNING, logger="spielplan"):
        cfg = Settings(public_url="", session_secret="", secrets_key=None)
    assert cfg.insecure_dev is True
    assert any("SPIELPLAN_INSECURE_DEV" in record.message for record in caplog.records)


def test_the_flag_is_off_unless_it_is_set(monkeypatch):
    """`conftest.py` pins `SPIELPLAN_INSECURE_DEV=0` and `model_config` reads `.env`, so both sources
    are removed to ask the field's own default."""
    monkeypatch.delenv("SPIELPLAN_INSECURE_DEV", raising=False)
    cfg = Settings(public_url="https://s.example.tld", session_secret=_SECRET, secrets_key=None,
                   _env_file=None)
    assert cfg.insecure_dev is False


class _FlagOnByDefault(Settings):
    """`core/config.py`'s field with its default flipped: the mutation the test above must catch."""

    insecure_dev: bool = Field(default=True, alias="SPIELPLAN_INSECURE_DEV")


def test_the_default_guard_sees_a_flag_that_defaults_to_on(monkeypatch, tmp_path):
    """Both sources carry the mutation, because either alone hides it."""
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
