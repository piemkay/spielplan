"""The one allow-list of image hosts this app may fetch cover art from. Decision 483.

No database: `art/hosts.py` is pure, and the art route and the importer's card resolution both
take their answer from it, so what is pinned here is what both of them serve and store.
"""

from __future__ import annotations

import pytest

from spielplan.art import hosts

W500 = "https://image.tmdb.org/t/p/w500/abc123.jpg"


def test_the_allow_list_is_tmdb_and_tvmaze_and_nothing_else():
    assert hosts.SERVABLE_HOSTS == ("image.tmdb.org", "static.tvmaze.com")


@pytest.mark.parametrize(
    "url",
    [W500, "https://static.tvmaze.com/uploads/images/medium_portrait/1/2.jpg"],
)
def test_an_https_url_on_a_named_host_is_servable(url):
    assert hosts.servable(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "/abc123.jpg",
        "http://image.tmdb.org/t/p/w500/abc123.jpg",
        "https://m.media-amazon.com/images/M/abc._V1_SX300.jpg",
        "https://evil.image.tmdb.org/t/p/w500/abc123.jpg",
        "https://image.tmdb.org.evil.com/t/p/w500/abc123.jpg",
        "https://image.tmdb.org:8443/t/p/w500/abc123.jpg",
        "https://user@image.tmdb.org/t/p/w500/abc123.jpg",
        "https://IMAGE.TMDB.ORG/t/p/w500/abc123.jpg",
        "https://[::1/t/p/w500/abc123.jpg",
    ],
)
def test_anything_else_is_refused(url):
    assert not hosts.servable(url)


def test_tmdb_size_rewrites_the_size_segment_and_never_prefixes():
    assert hosts.tmdb_size(W500) == "https://image.tmdb.org/t/p/w342/abc123.jpg"
    assert hosts.tmdb_size(W500, "original") == "https://image.tmdb.org/t/p/original/abc123.jpg"
    already = "https://image.tmdb.org/t/p/w342/abc123.jpg"
    assert hosts.tmdb_size(already) == already
    assert hosts.tmdb_size(hosts.tmdb_size(W500)) == already


@pytest.mark.parametrize(
    "url",
    [
        "https://static.tvmaze.com/uploads/images/medium_portrait/1/2.jpg",
        "https://image.tmdb.org/abc123.jpg",
        "/abc123.jpg",
        "https://m.media-amazon.com/t/p/w500/abc.jpg",
    ],
)
def test_tmdb_size_leaves_a_url_with_no_tmdb_size_segment_unchanged(url):
    assert hosts.tmdb_size(url) == url


def test_tmdb_size_refuses_a_size_tmdb_does_not_serve():
    with pytest.raises(ValueError):
        hosts.tmdb_size(W500, "w342/../x")
