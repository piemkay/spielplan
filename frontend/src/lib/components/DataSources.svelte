<script>
  /**
   * Data sources. Spec v2.1 §6.8, §10; decisions 293 and 298.
   *
   * WHY ONE BLOCK AND NOT A CREDIT LINE UNDER EVERY POSTER. Every install renders roughly ten
   * thousand TMDB overviews and posters, 8,418 IMDb scores, 8,409 Wikipedia plots and 1,619
   * TVmaze rows, and the licences behind them make a notice a condition of DISPLAYING the data
   * rather than a courtesy owed to it. What none of them requires is a credit on every tile, and
   * §6.8's quiet register argues against one: a source name stamped on a poster card is the
   * loudest thing on a screen whose subject is a film. So the notice is said once, where it can
   * be read. `PosterCard` and the shelves are left exactly as they are, and
   * `19-phone-shell.spec.js` asserts that they carry no source name — the absence is half the
   * rule and would otherwise be held by nothing. [decision 293]
   *
   * WHY /account AND NOT §6.6's ADMIN DATA CARD. A condition of display binds every member who
   * sees the overviews, not the one who imported them. `/admin/data` sits behind `AdminUser`
   * (`api/deps.py:280`), which would leave a household's non-admin member looking at TMDB text
   * with no reachable notice at all, and /account is the one surface the account chip routes to
   * for everybody, admin and member alike (`api/auth.py`'s `_nav` account list). It is NOT a tab:
   * `SURFACES` carries six entries and /account is not among them, which decision 318 corrects in
   * decision 293's argument and in this sentence. Nothing below branches on a role or reads the
   * session: the block is
   * the same markup for every signed-in member because there is no path on which it could
   * differ, which is why the browser half asserts reachability and not a role matrix.
   *
   * WHY THESE FIVE SENTENCES ARE WRITTEN HERE RATHER THAN READ OUT OF `rating_source`. Decision
   * 298 asks for the per-source licence text as well, and the corpus does ship it: migration
   * `0018_read_layer.sql` gave `rating_source` its `url`, `license`, `version` and `notes`
   * columns and `importer/load.py` maps all four. The one route that serves them is
   * `GET /api/admin/data/sources` (`api/admin.py`'s `data_sources`), and it is `AdminUser` too —
   * so reading it from here would render nothing for the members this block exists for, and
   * worse: `api/deps.py:283-289` answers an admin past §3.2's 24 hours with a 401 carrying
   * `X-Spielplan-Reauth: admin`, which `api.js` turns into the admin re-prompt. A member surface
   * that fetched an admin route would put an admin re-authentication prompt on the one page
   * every member reaches. A member-readable route is a backend change this surface does not get
   * to make, so the eleven datasets' own licence text stays on the admin card until one exists,
   * and the five notices below — which are the conditions of display, and are this app's own
   * strings rather than the corpus's — are stated in full.
   *
   * WIKIPEDIA AND TVMAZE ARE CREDITED, NOT DEEP-LINKED. CC BY-SA asks for the licence identified
   * and, where reasonable, the material linked — and this build holds no material to link. Nothing
   * under `backend/spielplan` or `frontend/src` reads `title_meta`'s `homepage`, `importer/load.py`
   * does not map the corpus's `wikipedia_title` onto `title`, and `/api/titles/{id}` carries no
   * field an anchor could be built from; for TVmaze the corpus column is the show's own marketing
   * site rather than the TVmaze page, so a link there would credit the wrong work under a CC BY-SA
   * notice. What this block owes is the credit and the licence, and both are here. The missing half
   * is not argued away in this comment: it is ruled on in decision 320 and published as a debt in
   * `docs/RELEASE.md` section 4.7, so a reader who wants the link has somewhere to find out why it
   * is absent. [decision 320]
   */

  /**
   * Decision 298's named slot. The TMDB logo is a trademark file the owner drops in from TMDB's
   * own brand page: no agent in this repository may fabricate or download one, and a hand-drawn
   * approximation would be a worse licence problem than a missing logo. So the block renders the
   * slot when `frontend/static/tmdb-logo.svg` is in the tree and nothing at all when it is not,
   * and `docs/RELEASE.md` carries the absence as an owed asset.
   *
   * Resolved by a build-time glob rather than by an `<img>` with an `onerror`, because the
   * fallback shape asks the browser for a file that is not there on every visit to /account and
   * then hides the broken result — a request the network log records as this app's own 404. The
   * glob is evaluated when the bundle is built and yields an empty object today; the served path
   * is the static one, since SvelteKit publishes `static/` at the root.
   */
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
      <!-- Verbatim, because the words are the permission: "courtesy of IMDb" with the address
           dropped is a different claim from the one that was granted. -->
      <p class="why" data-notice="imdb">
        Information courtesy of IMDb (https://www.imdb.com). Used with permission.
      </p>
    </li>

    <li>
      <span class="data">TMDB</span>
      {#if TMDB_LOGO}
        <img class="logo" src={TMDB_LOGO} alt="TMDB" />
      {/if}
      <!-- Verbatim for the same reason IMDb's is, and for a milestone it was not. TMDB's API
           terms section 3 fixes this sentence and licenses exactly one edit in it: the bracketed
           "[website, program, service, application, product]" is a choose-one and nothing else in
           it is ours to write. What shipped was "uses the TMDB API" - the head of TMDB's
           developer-FAQ form welded to the tail of the terms form, a quotation of neither - and
           it dropped the clause that is true of this build for one that is not, since no code
           here calls TMDB: the overviews and poster paths arrive inside the corpus bundle.
           [decision 319] -->
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
  /* The card's own box is design.css's (`--card-pad` and the skin with it); what belongs here is
     the stack inside it, which this component has to set for itself because /account's `.card`
     rule is scoped to that page and does not reach a child component's markup. */
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
  /* UNDERLINED RATHER THAN EMBER, WHICH IS THE OPPOSITE OF WHAT IT LOOKED LIKE IT WANTED.
     design.css's global `a` rule is (0,0,1) and `.data` is (0,1,0), so a link wearing the data
     voice loses the accent and stops reading as something to tap; the obvious repair is to take
     it back in this file. That repair is exactly what decision 276 rations: §6.8 spends the one
     accent on selection and primary actions, and a licence deed is neither — it is an annotation
     beside a name, which is what `.data` already says it is. The affordance a quiet register has
     for that is the underline, which costs no colour at all. The accent stays where it means
     something by being rare. [§6.8; decision 276] */
  .licence {
    display: inline-block;
    text-decoration: underline;
  }

  @media (pointer: coarse) {
    /* §6 preamble's 48 px floor. design.css's coarse block raises `button`, `select` and the
       three button-shaped classes and never a bare `<a>`, so a link that wants the floor has to
       ask for it — and in both dimensions, because that block raises `min-height` and never
       `min-width`: two overlay exits shipped 48 by 32 under a height-only sweep, which is why
       `19-phone-shell.spec.js`'s `meetsTheTouchFloor` measures the narrow axis too.
       `line-height` and not a flex centre: `min-height` does not apply to an inline box at all,
       so the display has to change to hold the floor, and an inline-block underlines its own
       text in every engine while a decoration set on a flex container is propagated into its
       items by some and not by others. The affordance may not be the thing that is engine-
       dependent. */
    .licence {
      min-height: var(--touch);
      min-width: var(--touch);
      line-height: var(--touch);
    }
  }
</style>
