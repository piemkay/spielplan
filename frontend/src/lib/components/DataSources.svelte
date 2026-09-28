<script>
  // One notice block on /account, not a credit per poster: the licences make a notice a condition
  // of display, and /account is the one page every member reaches. The sentences live here because
  // the route serving `rating_source`'s licence text is admin-only.

  // The owner drops TMDB's own logo file in; none is fabricated here. A build-time glob, not an
  // <img> with onerror, so an absent logo costs no request.
  const TMDB_LOGO = Object.keys(import.meta.glob('/static/tmdb-logo.svg')).length
    ? '/tmdb-logo.svg'
    : null;
</script>

<section class="sources" data-testid="data-sources">
  <h2 class="list-header">Data sources</h2>
  <ul class="list-group">
    <li>
      <span class="name">IMDb</span>
      <!-- Verbatim: the words are the permission. -->
      <p class="notice" data-notice="imdb">
        Information courtesy of IMDb (https://www.imdb.com). Used with permission.
      </p>
    </li>

    <li>
      <span class="name">TMDB</span>
      {#if TMDB_LOGO}
        <img class="logo" src={TMDB_LOGO} alt="TMDB" />
      {/if}
      <!-- Verbatim: TMDB's terms fix the sentence and license only the bracketed choice. -->
      <p class="notice" data-notice="tmdb">
        This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise
        approved by TMDB.
      </p>
    </li>

    <li>
      <span class="name">Wikipedia</span>
      <p class="notice" data-notice="wikipedia">
        Plot summaries and overviews from Wikipedia, by its contributors, under CC BY-SA 4.0.
      </p>
      <a class="licence" href="https://creativecommons.org/licenses/by-sa/4.0/" rel="noreferrer">
        CC BY-SA 4.0
      </a>
    </li>

    <li>
      <span class="name">TVmaze</span>
      <p class="notice" data-notice="tvmaze">Series data from TVmaze, under CC BY-SA 4.0.</p>
      <a class="licence" href="https://creativecommons.org/licenses/by-sa/4.0/" rel="noreferrer">
        CC BY-SA 4.0
      </a>
    </li>

    <li>
      <span class="name">OMDb</span>
      <p class="notice" data-notice="omdb">Ratings and plot text from OMDb, under CC BY-NC 4.0.</p>
      <a class="licence" href="https://creativecommons.org/licenses/by-nc/4.0/" rel="noreferrer">
        CC BY-NC 4.0
      </a>
    </li>
  </ul>
  <p class="list-footer">
    The posters, overviews and scores in Spielplan are other people's work, shared on terms that
    ask for a credit inside the app. These are those credits.
  </p>
</section>

<style>
  .sources {
    display: flex;
    flex-direction: column;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 12px var(--gutter);
  }
  li + li {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .notice {
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .logo {
    height: 13px;
    width: auto;
    margin: 4px 0;
  }
  /* Underlined, not the accent: a licence link is an annotation, not an action (§6.8). */
  .licence {
    display: inline-block;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
    text-decoration: underline;
  }

  @media (pointer: coarse) {
    /* design.css gives a bare <a> no coarse floor; inline-block, since min-height skips inline boxes. */
    .licence {
      min-height: var(--touch);
      min-width: var(--touch);
      line-height: var(--touch);
    }
  }
</style>
