PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    canonical_json TEXT NOT NULL,
    fetched_at TEXT,
    is_private INTEGER NOT NULL DEFAULT 1 CHECK (is_private IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (source, sha256)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
    actor_run_id TEXT,
    dataset_id TEXT,
    plan_fingerprint TEXT,
    next_offset INTEGER NOT NULL DEFAULT 0 CHECK (next_offset >= 0),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    input_json TEXT,
    metadata_json TEXT,
    stats_json TEXT,
    error TEXT,
    UNIQUE (source, actor_run_id)
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY,
    place_key TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    country_code TEXT,
    region TEXT,
    locality TEXT,
    address TEXT,
    latitude REAL CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    longitude REAL CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    location_scope TEXT NOT NULL DEFAULT 'unknown'
        CHECK (location_scope IN ('budapest', 'outside-budapest', 'foreign', 'unknown')),
    starts_in_budapest INTEGER NOT NULL DEFAULT 0
        CHECK (starts_in_budapest IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE RESTRICT,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
    external_id TEXT NOT NULL CHECK (length(trim(external_id)) > 0),
    url TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'unknown' CHECK (length(trim(kind)) > 0),
    description TEXT,
    location_text TEXT,
    rating REAL CHECK (rating IS NULL OR rating BETWEEN 0 AND 5),
    review_count INTEGER CHECK (review_count IS NULL OR review_count >= 0),
    price_from REAL CHECK (price_from IS NULL OR price_from >= 0),
    currency TEXT,
    duration_text TEXT,
    location_scope TEXT NOT NULL DEFAULT 'unknown'
        CHECK (location_scope IN ('budapest', 'outside-budapest', 'foreign', 'unknown')),
    starts_in_budapest INTEGER NOT NULL DEFAULT 0
        CHECK (starts_in_budapest IN (0, 1)),
    latest_raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id) ON DELETE RESTRICT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS listing_snapshots (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id) ON DELETE RESTRICT,
    scraped_at TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'unknown' CHECK (length(trim(kind)) > 0),
    description TEXT,
    url TEXT,
    rating REAL CHECK (rating IS NULL OR rating BETWEEN 0 AND 5),
    review_count INTEGER CHECK (review_count IS NULL OR review_count >= 0),
    price_from REAL CHECK (price_from IS NULL OR price_from >= 0),
    currency TEXT,
    location_scope TEXT NOT NULL
        CHECK (location_scope IN ('budapest', 'outside-budapest', 'foreign', 'unknown')),
    starts_in_budapest INTEGER NOT NULL CHECK (starts_in_budapest IN (0, 1)),
    UNIQUE (listing_id, raw_payload_id)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listing_categories (
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (listing_id, category_id)
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    external_id TEXT,
    media_type TEXT NOT NULL DEFAULT 'image',
    url TEXT NOT NULL,
    caption TEXT,
    width INTEGER CHECK (width IS NULL OR width > 0),
    height INTEGER CHECK (height IS NULL OR height > 0),
    sort_order INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (listing_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    external_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    price REAL CHECK (price IS NULL OR price >= 0),
    original_price REAL CHECK (original_price IS NULL OR original_price >= 0),
    currency TEXT,
    duration_text TEXT,
    availability_text TEXT,
    url TEXT,
    provider TEXT,
    category TEXT,
    sort_order INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (listing_id, dedupe_key)
);

-- Reviewer names, profile URLs, avatars and user IDs intentionally never enter
-- this normalized table. They remain only inside private raw_payloads.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id) ON DELETE RESTRICT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    rating REAL CHECK (rating IS NULL OR rating BETWEEN 0 AND 5),
    title TEXT,
    body TEXT,
    language TEXT,
    review_date TEXT,
    helpful_count INTEGER CHECK (helpful_count IS NULL OR helpful_count >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS listing_enrichments (
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    enrichment_kind TEXT NOT NULL,
    enrichment_version TEXT NOT NULL,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id) ON DELETE RESTRICT,
    enriched_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, enrichment_kind, enrichment_version)
);

CREATE TABLE IF NOT EXISTS listing_enrichment_attempts (
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    enrichment_kind TEXT NOT NULL,
    enrichment_version TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'not-returned', 'failed')),
    requested_url TEXT,
    attempted_at TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (listing_id, enrichment_kind, enrichment_version, run_id)
);

CREATE TABLE IF NOT EXISTS scrape_run_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    external_id TEXT,
    url TEXT,
    query_label TEXT,
    destination TEXT,
    result_rank INTEGER CHECK (result_rank IS NULL OR result_rank >= 0),
    metadata_json TEXT,
    status TEXT NOT NULL DEFAULT 'stored'
        CHECK (status IN ('stored', 'skipped', 'failed')),
    raw_payload_id INTEGER REFERENCES raw_payloads(id) ON DELETE RESTRICT,
    listing_id INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    error TEXT,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_raw_payloads_sha ON raw_payloads(sha256);
CREATE INDEX IF NOT EXISTS idx_runs_source_started ON scrape_runs(source, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_plan
    ON scrape_runs(plan_fingerprint)
    WHERE status = 'running' AND plan_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_run_items_listing ON scrape_run_items(listing_id);
CREATE INDEX IF NOT EXISTS idx_places_scope ON places(location_scope);
CREATE INDEX IF NOT EXISTS idx_listings_scope_quality
    ON listings(location_scope, active, rating DESC, review_count DESC);
CREATE INDEX IF NOT EXISTS idx_listings_kind_scope_quality
    ON listings(kind, location_scope, active, rating DESC, review_count DESC);
CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source, active);
CREATE INDEX IF NOT EXISTS idx_snapshots_listing_time
    ON listing_snapshots(listing_id, scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_listing_active ON media(listing_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_packages_listing_active ON packages(listing_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_reviews_listing_date ON reviews(listing_id, review_date DESC);
CREATE INDEX IF NOT EXISTS idx_enrichments_kind_time
    ON listing_enrichments(enrichment_kind, enriched_at DESC);
CREATE INDEX IF NOT EXISTS idx_enrichment_attempts_lookup
    ON listing_enrichment_attempts(
        enrichment_kind, enrichment_version, status, listing_id
    );

CREATE VIRTUAL TABLE IF NOT EXISTS listing_fts USING fts5(
    title,
    description,
    location_text,
    content='listings',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS listings_ai AFTER INSERT ON listings BEGIN
    INSERT INTO listing_fts(rowid, title, description, location_text)
    VALUES (new.id, new.title, new.description, new.location_text);
END;

CREATE TRIGGER IF NOT EXISTS listings_ad AFTER DELETE ON listings BEGIN
    INSERT INTO listing_fts(listing_fts, rowid, title, description, location_text)
    VALUES ('delete', old.id, old.title, old.description, old.location_text);
END;

CREATE TRIGGER IF NOT EXISTS listings_au AFTER UPDATE ON listings BEGIN
    INSERT INTO listing_fts(listing_fts, rowid, title, description, location_text)
    VALUES ('delete', old.id, old.title, old.description, old.location_text);
    INSERT INTO listing_fts(rowid, title, description, location_text)
    VALUES (new.id, new.title, new.description, new.location_text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS review_fts USING fts5(
    title,
    body,
    language,
    content='reviews',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS reviews_ai AFTER INSERT ON reviews BEGIN
    INSERT INTO review_fts(rowid, title, body, language)
    VALUES (new.id, new.title, new.body, new.language);
END;

CREATE TRIGGER IF NOT EXISTS reviews_ad AFTER DELETE ON reviews BEGIN
    INSERT INTO review_fts(review_fts, rowid, title, body, language)
    VALUES ('delete', old.id, old.title, old.body, old.language);
END;

CREATE TRIGGER IF NOT EXISTS reviews_au AFTER UPDATE ON reviews BEGIN
    INSERT INTO review_fts(review_fts, rowid, title, body, language)
    VALUES ('delete', old.id, old.title, old.body, old.language);
    INSERT INTO review_fts(rowid, title, body, language)
    VALUES (new.id, new.title, new.body, new.language);
END;

CREATE VIEW IF NOT EXISTS listing_quality_ranking AS
WITH prior AS (
    SELECT
        4.0 AS mean_rating,
        50.0 AS weight
)
SELECT
    l.id AS listing_id,
    l.source,
    l.external_id,
    l.title,
    l.location_scope,
    l.starts_in_budapest,
    l.rating,
    COALESCE(l.review_count, 0) AS review_count,
    prior.mean_rating AS prior_mean,
    prior.weight AS prior_weight,
    CASE
        WHEN l.rating IS NULL THEN NULL
        ELSE ROUND(
            ((COALESCE(l.review_count, 0) * l.rating) +
             (prior.weight * prior.mean_rating)) /
            (COALESCE(l.review_count, 0) + prior.weight),
            6
        )
    END AS bayesian_rating
FROM listings AS l
CROSS JOIN prior
WHERE l.active = 1;

PRAGMA user_version = 6;
