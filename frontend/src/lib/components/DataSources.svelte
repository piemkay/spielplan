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

<section class="card" data-testid="data-sources">
  <h2>Data sources</h2>
  <p class="why">
    The posters, overviews, scores and articles on these screens are other people's work, shown
    here under terms that ask for a line of credit inside the app. This is that line.
  </p>

  <ul>
    <li>
      <span class="data">IMDb</span>
      <!-- Verbatim: the words are the permission. -->
      <p class="why" data-notice="imdb">
        Information courtesy of IMDb (https://www.imdb.com). Used with permission.
      </p>
    </li>

    <li>
      <span class="data">TMDB</span>
      {#if TMDB_LOGO}
        <img class="logo" src={TMDB_LOGO} alt="TMDB" />
      {/if}
      <!-- Verbatim: TMDB's terms fix the sentence and license only the bracketed choice. -->
      <p class="why" data-notice="tmdb">
        This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise
        approved by TMDB.
      </p>
    </li>

    <li>
      <span class="data">Wikipedia</span>
      <p class="why" data-notice="wikipedia">
        Plot summaries and overviews from Wikipedia, by its contributors, under CC BY-SA 4.0.
      </p>
      <a class="licence data" href="https://creativecommons.org/licenses/by-sa/4.0/" rel="noreferrer">
        CC BY-SA 4.0
      </a>
    </li>

    <li>
      <span class="data">TVmaze</span>
      <p class="why" data-notice="tvmaze">Series data from TVmaze, under CC BY-SA 4.0.</p>
      <a class="licence data" href="https://creativecommons.org/licenses/by-sa/4.0/" rel="noreferrer">
        CC BY-SA 4.0
      </a>
    </li>

    <li>
      <span class="data">OMDb</span>
      <p class="why" data-notice="omdb">Ratings and plot text from OMDb, under CC BY-NC 4.0.</p>
      <a class="licence data" href="https://creativecommons.org/licenses/by-nc/4.0/" rel="noreferrer">
        CC BY-NC 4.0
      </a>
    </li>
  </ul>
</section>

<style>
  /* /account's scoped `.card` rule does not reach this child, so the stack is set here. */
  section {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  li {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 3px;
  }
  .why {
    margin: 0;
  }
  .logo {
    height: 13px;
    width: auto;
  }
  /* Underlined, not the accent: a licence link is an annotation, not a selection (§6.8). */
  .licence {
    display: inline-block;
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
