"""The poster cache on disk. Spec v2.1 §6.8; decision 483.

No database and no network: `art/cache.py` is files and a clock, and the clock is injected so a
180-day re-fetch is asserted rather than waited for.
"""

from __future__ import annotations

from spielplan.art import cache

PNG = b"\x89PNG\r\n\x1a\nnot-really-but-enough"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_a_stored_poster_is_read_back_with_its_type_and_a_strong_etag(tmp_path):
    clock = Clock()
    store = cache.ArtCache(tmp_path / "art", clock=clock)
    stored = store.store(7, "sig-a", PNG, content_type="image/png", source="poster_path", ttl=60)
    hit = store.read(7, "sig-a")
    assert hit == stored
    assert hit.status == cache.OK and hit.content_type == "image/png"
    assert hit.etag.startswith('"') and hit.etag.endswith('"')
    assert store.bytes_of(7) == PNG


def test_an_entry_answers_only_the_sources_it_was_fetched_from(tmp_path):
    """The URL names a title, not a file: a title that became owned or was re-derived is a new
    question with the same key, and a stale answer for 180 days is what the signature prevents."""
    store = cache.ArtCache(tmp_path, clock=Clock())
    store.store(7, "https://image.tmdb.org/t/p/w342/a.jpg", PNG, content_type="image/png",
                source="poster_path", ttl=60)
    assert store.read(7, "jellyfin:jf-1|https://image.tmdb.org/t/p/w342/a.jpg") is None
    assert store.read(7, "https://image.tmdb.org/t/p/w342/a.jpg") is not None


def test_an_entry_expires_at_its_ttl(tmp_path):
    clock = Clock()
    store = cache.ArtCache(tmp_path, clock=clock)
    store.store(7, "s", PNG, content_type="image/png", source="poster_path", ttl=180 * 86400)
    clock.now += 180 * 86400 - 1
    assert store.read(7, "s") is not None
    clock.now += 1
    assert store.read(7, "s") is None


def test_a_negative_answer_is_remembered_and_takes_the_old_bytes_with_it(tmp_path):
    clock = Clock()
    store = cache.ArtCache(tmp_path, clock=clock)
    store.store(7, "s", PNG, content_type="image/png", source="poster_path", ttl=60)
    store.store_negative(7, "s", status=cache.MISSING, ttl=600)
    hit = store.read(7, "s")
    assert hit.status == cache.MISSING and hit.content_type is None
    assert not (tmp_path / "7.img").exists(), "a `missing` sidecar beside old bytes is two answers"
    clock.now += 600
    assert store.read(7, "s") is None


def test_damage_to_the_cache_is_a_miss_and_never_an_error(tmp_path):
    """Droppable by construction: the worst a corrupt sidecar or a lost image can cost is a fetch."""
    store = cache.ArtCache(tmp_path, clock=Clock())
    assert store.read(7, "s") is None
    (tmp_path / "7.json").write_text("{not json", encoding="utf-8")
    assert store.read(7, "s") is None
    store.store(8, "s", PNG, content_type="image/png", source="poster_path", ttl=60)
    (tmp_path / "8.img").unlink()
    assert store.read(8, "s") is None, "an ok sidecar with no bytes beside it vouches for nothing"
    assert not list(tmp_path.glob("*.tmp")), "every write lands whole or not at all"
