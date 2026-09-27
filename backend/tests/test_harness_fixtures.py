"""The `app` fixture's isolation from the operator's environment and `.env`."""

from __future__ import annotations

import os

import pytest

from tests import conftest


@pytest.fixture
def a_dot_env_in_the_working_directory(tmp_path, monkeypatch):
    """Never the repository root: this fixture must not overwrite a real `.env`."""
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".env").write_text(
        "JELLYFIN_URL=http://jellyfin.invalid\n"
        "JELLYFIN_API_KEY=an-operators-real-admin-key\n"
        "SECRETS_KEY=an-operators-secrets-key-long-enough-to-pass\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(home)
    return home


async def test_the_app_fixture_ignores_a_dot_env_in_the_working_directory(
    a_dot_env_in_the_working_directory, db, app
):
    """The `app` fixture runs the genuine lifespan, which seeds connectors from the environment."""
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the lifespan seeded a connector from the .env in pytest's working directory"
    )


@pytest.fixture
def every_connector_seeded_in_the_environment(monkeypatch):
    """Both spellings: a POSIX environment keeps a lower-case seed apart."""
    names = conftest._connector_seed_env_names()
    assert names, "Settings should declare the connector seed fields"
    monkeypatch.setattr(os, "environ", dict(os.environ))
    spellings = [*names, *(name.lower() for name in names)]
    for name in spellings:
        monkeypatch.setenv(name, f"set-by-the-operators-environment-{name}")
    return spellings


async def test_the_app_fixture_clears_every_connector_seed_variable(
    secrets_key, every_connector_seeded_in_the_environment, db, app
):
    """Derived from `Settings`, so a new connector variable arrives here already neutralised."""
    for name in every_connector_seeded_in_the_environment:
        assert os.environ.get(name) is None, f"{name} reached the app fixture"
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the lifespan seeded connectors from the environment pytest inherited"
    )
