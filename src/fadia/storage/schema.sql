-- Postgres como fuente de verdad. JSONB para lo que varía por tienda,
-- columnas tipadas para lo que se filtra y se ordena.
--
-- Por qué no Mongo: el 90% de las consultas de este sistema son
-- "producto -> sus variantes -> su histórico de precio", que es un join
-- de tres niveles, y la pregunta que da valor al dataset ("qué bajó de
-- precio esta semana") es una ventana sobre una serie temporal. Eso en
-- un store de documentos se paga en agregaciones a mano y en pérdida de
-- integridad referencial. La heterogeneidad entre tiendas —el argumento
-- real a favor de NoSQL— se resuelve con `raw JSONB`.

CREATE EXTENSION IF NOT EXISTS vector;      -- búsqueda semántica
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy match de títulos

-- ---------------------------------------------------------------- tiendas
CREATE TABLE store (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    domain      TEXT NOT NULL UNIQUE,
    platform    TEXT NOT NULL,
    country     CHAR(2) NOT NULL DEFAULT 'AR',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    crawl_delay REAL NOT NULL DEFAULT 1.0,
    last_crawl  TIMESTAMPTZ
);

-- ---------------------------------------------------------------- productos
CREATE TABLE product (
    product_uid   TEXT PRIMARY KEY,          -- 'sunnyclothing:232256855'
    store_slug    TEXT NOT NULL REFERENCES store(slug) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,
    url           TEXT NOT NULL,
    handle        TEXT NOT NULL,

    title            TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    description      TEXT,
    brand            TEXT,

    category      TEXT,                      -- taxonomía canónica
    category_path TEXT[] NOT NULL DEFAULT '{}',
    gender        TEXT NOT NULL DEFAULT 'unknown',
    collections   TEXT[] NOT NULL DEFAULT '{}',
    tags          TEXT[] NOT NULL DEFAULT '{}',

    price_min_cents  BIGINT NOT NULL,        -- desnormalizado: se ordena por acá
    price_max_cents  BIGINT NOT NULL,
    price_retail_cents BIGINT,                -- 'precio por menor' en mayoristas
    min_purchase     TEXT,                    -- texto libre del local
    min_qty          INTEGER,
    min_unit         TEXT,
    seller_name      TEXT,                    -- marketplace: 3.856 locales
    seller_address   TEXT,
    seller_verified  BOOLEAN,
    seller_followers INTEGER,
    availability     TEXT NOT NULL DEFAULT 'unknown',
    sizes_available  TEXT[] NOT NULL DEFAULT '{}',
    colors_available TEXT[] NOT NULL DEFAULT '{}',
    images           JSONB NOT NULL DEFAULT '[]',

    content_hash  TEXT,                      -- si no cambió, no se reescribe nada
    embedding     vector(768),               -- nomic-embed-text-v1.5
    raw           JSONB NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    scraped_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_slug, external_id)
);

CREATE INDEX product_category_idx  ON product (category, gender, availability);
CREATE INDEX product_price_idx     ON product (price_min_cents) WHERE availability = 'in_stock';
CREATE INDEX product_title_trgm    ON product USING gin (title_normalized gin_trgm_ops);
CREATE INDEX product_collections   ON product USING gin (collections);
CREATE INDEX product_embedding_idx ON product USING hnsw (embedding vector_cosine_ops);
CREATE INDEX product_seller_idx    ON product (seller_name) WHERE seller_name IS NOT NULL;
CREATE INDEX product_store_idx     ON product (store_slug, availability);

-- ---------------------------------------------------------------- variantes
CREATE TABLE variant (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_uid   TEXT NOT NULL REFERENCES product(product_uid) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,
    sku           TEXT,

    size_raw        TEXT,
    size_normalized TEXT,
    size_system     TEXT,
    color_raw        TEXT,
    color_normalized TEXT,

    price_cents       BIGINT NOT NULL,
    compare_at_cents  BIGINT,
    availability      TEXT NOT NULL DEFAULT 'unknown',
    stock             INTEGER,
    image_url         TEXT,
    extra_options     JSONB NOT NULL DEFAULT '{}',
    UNIQUE (product_uid, external_id)
);

CREATE INDEX variant_size_idx  ON variant (size_normalized) WHERE availability = 'in_stock';
CREATE INDEX variant_color_idx ON variant (color_normalized) WHERE availability = 'in_stock';

-- ---------------------------------------------------------------- histórico
-- Append-only. Es la tabla que convierte un scraper en un dataset:
-- sin ella solo sabés el precio de hoy, que ya lo sabe la tienda.
CREATE TABLE price_point (
    product_uid  TEXT NOT NULL,
    variant_external_id TEXT NOT NULL,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_cents  BIGINT NOT NULL,
    compare_at_cents BIGINT,
    availability TEXT NOT NULL,
    stock        INTEGER,
    PRIMARY KEY (product_uid, variant_external_id, observed_at)
) PARTITION BY RANGE (observed_at);

-- Una partición por mes: con inflación AR vas a escribir mucho y
-- consultar casi siempre los últimos 30 días. La DEFAULT evita que un
-- insert fuera de rango tire el crawl entero a las 3 de la mañana.
CREATE TABLE price_point_2026_08 PARTITION OF price_point
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE price_point_2026_09 PARTITION OF price_point
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE price_point_default PARTITION OF price_point DEFAULT;

-- ---------------------------------------------------------------- auditoría
CREATE TABLE crawl_run (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_slug  TEXT NOT NULL REFERENCES store(slug),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    discovered  INTEGER NOT NULL DEFAULT 0,
    parsed_ok   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    notes       JSONB NOT NULL DEFAULT '{}'
);
