"""Cover art, served from this app's own origin. Spec v2.1 §6.8 ("Poster-forward 2:3 cards");
decision 483.

`hosts` is the one owner of the third-party image hosts this app may fetch from, read by the art
route that serves `/api/art/{title_id}/poster` and by the importer's card resolution alike, so the
allow-list the page serves and the allow-list the import stores cannot drift apart.
"""
