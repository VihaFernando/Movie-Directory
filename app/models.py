from pydantic import BaseModel


class Movie(BaseModel):
    title: str
    thumbnail_url: str
    detail_page_slug: str
    source: str
    content_type: str


class CatalogResponse(BaseModel):
    count: int
    movies: list[Movie]
    source: str
    content_type: str


class FavoriteIn(BaseModel):
    """Body for POST /api/favorites - just enough to identify the title and
    render it in the favorites grid without a round-trip back through the
    catalog/detail scrapers."""
    slug: str
    source: str
    content_type: str
    title: str = ""
    poster_url: str = ""
    year: str = ""
    rating: float | None = None


class WatchProgressIn(BaseModel):
    """Body for POST /api/history - a playback checkpoint for one title.
    Sent periodically while playing (throttled client-side) and once more
    on pause/unload, so a resumed session picks up close to where playback
    actually stopped rather than only at natural save points."""
    slug: str
    source: str
    content_type: str
    title: str = ""
    poster_url: str = ""
    backdrop_url: str = ""
    year: str = ""
    season: int | None = None
    episode: int | None = None
    progress_seconds: float = 0
    duration_seconds: float = 0


class EmbedResponse(BaseModel):
    slug: str
    # Echoed back for TV so the frontend can tell which episode this stream
    # belongs to - a late response for a previously-selected episode would
    # otherwise be indistinguishable from the current one.
    season: int | None = None
    episode: int | None = None
    embed_url: str | None
    proxied_url: str | None  # convenience: ready-to-use local proxy URL for the frontend
    is_media: bool = False  # True if proxied_url is a direct video file (use <video>, not <iframe>)
    is_hls: bool = False  # True if proxied_url is an HLS playlist (.m3u8) - needs hls.js in most browsers


class Episode(BaseModel):
    """One episode of a series season.

    `episode_number`/`season_number` are what actually drive playback (the
    source addresses an episode by its S/E pair, not by any id), so they are
    required; everything else is presentational and commonly missing for
    unaired or sparsely-catalogued episodes.
    """
    season_number: int
    episode_number: int
    title: str
    overview: str = ""
    still_url: str = ""
    air_date: str = ""
    runtime: int | None = None


class Season(BaseModel):
    """A season entry from the series' season list.

    `episode_count` comes from the series payload and is used to render the
    season picker before that season's episodes have been fetched - the
    `episodes` list stays empty until a caller asks for that season
    specifically, since the source only renders one season at a time.
    """
    season_number: int
    name: str
    episode_count: int = 0
    overview: str = ""
    poster_url: str = ""
    air_date: str = ""
    episodes: list[Episode] = []


class CastMember(BaseModel):
    """One billed actor, from TMDB's credits.cast - just enough to render a
    cast strip (photo, name, character); TMDB's full person object carries
    far more (biography, known_for, external ids) that this app has no UI
    for yet."""
    name: str
    character: str = ""
    profile_url: str = ""


class SimilarTitle(BaseModel):
    """A related title for the "More Like This" row. Deliberately a subset
    of Movie's fields (no source/detail_page_slug) since a similar title's
    id IS its slug on this source - see TitleDetail.slug's docstring on the
    module this feeds, app/tmdb.py."""
    slug: str
    title: str
    poster_url: str = ""
    rating: float | None = None
    year: str = ""
    content_type: str = "movie"


class TitleDetail(BaseModel):
    """Everything the detail page renders for one title.

    Covers movies and series alike - `seasons` is simply empty for a movie,
    which is what lets the frontend render one page for both. The fields
    below the separator are what the hero banner needs (backdrop, poster,
    rating, year, genres); they come from the source's own embedded TMDB
    payload, so no extra API or key is involved.
    """
    slug: str
    title: str
    source: str
    content_type: str = "movie"
    overview: str = ""

    # --- hero presentation ---
    backdrop_url: str = ""
    poster_url: str = ""
    # TMDB's 0-10 average. Optional because an unreleased or obscure title
    # genuinely has no rating, which must render as absent rather than 0.0.
    rating: float | None = None
    # Release year for a movie, first-air year for a series.
    year: str = ""
    genres: list[str] = []
    tagline: str = ""
    # Minutes for a movie; a series' typical episode runtime. None when TMDB
    # has no runtime for this title (common for an unreleased or obscure
    # one) or when the detail came from the scrape fallback, which doesn't
    # parse it out of the page payload.
    runtime: int | None = None
    # TMDB's own lifecycle label (e.g. "Released", "Ended", "Returning
    # Series") - shown as-is rather than reinterpreted, since its exact set
    # of values differs between movies and series.
    status: str = ""
    cast: list[CastMember] = []
    # Bare YouTube video id (not a URL) for the first official trailer TMDB
    # lists, so the frontend builds the embed URL itself and can swap
    # players later without a backend change.
    trailer_key: str = ""
    similar: list[SimilarTitle] = []

    # --- series only ---
    seasons: list[Season] = []
    # The season whose `episodes` are populated in this response.
    selected_season: int | None = None


# Kept as an alias so the earlier name still resolves - the series endpoint
# and its scraper were written against it before the detail page existed.
SeriesDetail = TitleDetail
