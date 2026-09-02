"""Backup and restore. Spec v2.1 §2 (Backups), §10; decision 162.

Two artifacts, on purpose.

`nightly` is §2's job: "nightly `pg_dump` to `/data/backups`, rotation 14". It is the whole
database — user state and all — and its contract is a negative one, because the connector
secrets in it are only ever ciphertext.

`movie_data` is the other artifact, and it exists because of decision 162: "movie data is
exported once by a compatible exporter and imported once … Spielplan owns all ids from the seed
onwards." The corpus is no longer a place the content can be fetched from again, so the
household's copy is the only copy and has to be extractable on its own — without the user state,
and restorable into a fresh install.
"""

from spielplan.backup import movie_data, nightly

__all__ = ["movie_data", "nightly"]
