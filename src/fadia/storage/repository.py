"""Persistencia en Postgres.

Dos decisiones que se pagan solas:

- **Upsert por `content_hash`.** Si la ficha no cambió, no se reescribe el
  producto ni sus variantes. Con 139.044 fichas y crawls diarios, la
  diferencia es escribir ~2.000 filas por día en vez de 400.000.
- **`price_point` siempre se escribe**, cambie o no el precio. Es
  append-only y es lo que convierte un scraper en un dataset: sin la serie
  temporal solo sabés el precio de hoy, que ya lo sabe la tienda.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..models import Product, Store

PRODUCT_COLS = (
    "product_uid", "store_slug", "external_id", "url", "handle",
    "title", "title_normalized", "description", "brand",
    "category", "category_path", "gender", "collections", "tags",
    "price_min_cents", "price_max_cents", "price_retail_cents",
    "min_purchase", "min_qty", "min_unit",
    "seller_name", "seller_address", "seller_verified", "seller_followers",
    "availability", "sizes_available", "colors_available", "images",
    "content_hash", "raw", "scraped_at",
)

VARIANT_COLS = (
    "product_uid", "external_id", "sku",
    "size_raw", "size_normalized", "size_system",
    "color_raw", "color_normalized",
    "price_cents", "compare_at_cents", "availability", "stock",
    "image_url", "extra_options",
)


def _product_row(p: Product) -> tuple:
    s = p.seller
    return (
        p.product_uid, p.store_slug, p.external_id, str(p.url), p.handle,
        p.title, p.title_normalized, p.description, p.brand,
        p.category, p.category_path, p.gender.value, p.collections, p.tags,
        p.price_min.amount_cents, p.price_max.amount_cents,
        p.price_retail.amount_cents if p.price_retail else None,
        p.min_purchase, p.min_qty, p.min_unit,
        s.name if s else None, s.address if s else None,
        s.verified if s else None, s.followers if s else None,
        p.availability.value, p.sizes_available, p.colors_available,
        json.dumps([i.model_dump(mode="json") for i in p.images]),
        p.content_hash, json.dumps(p.raw, default=str), p.scraped_at,
    )


def _variant_rows(p: Product) -> list[tuple]:
    out = []
    for v in p.variants:
        out.append((
            p.product_uid, v.external_id, v.sku,
            v.size.raw if v.size else None,
            v.size.normalized if v.size else None,
            v.size.system.value if v.size else None,
            v.color.raw if v.color else None,
            v.color.normalized if v.color else None,
            v.price.amount_cents, v.price.compare_at_cents,
            v.availability.value, v.stock,
            str(v.image_url) if v.image_url else None,
            json.dumps(v.extra_options),
        ))
    return out


class Repository:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, open=True)

    def close(self) -> None:
        self.pool.close()

    # ------------------------------------------------------------------ tiendas
    def upsert_store(self, store: Store) -> None:
        with self.pool.connection() as conn:
            conn.execute("""
                INSERT INTO store (slug, name, domain, platform, country)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                   SET name = EXCLUDED.name,
                       domain = EXCLUDED.domain,
                       platform = EXCLUDED.platform
            """, (store.slug, store.name, store.domain, store.platform, store.country))

    # ------------------------------------------------------------------ productos
    def existing_hashes(self, store_slug: str) -> dict[str, str]:
        """`{product_uid: content_hash}` de lo ya guardado, para saltear lo intacto."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT product_uid, content_hash FROM product WHERE store_slug = %s",
                (store_slug,)).fetchall()
        return {r["product_uid"]: r["content_hash"] for r in rows}

    def upsert_products(self, products: Sequence[Product]) -> dict[str, int]:
        """Escribe producto + variantes + punto de precio. Devuelve conteos."""
        if not products:
            return {"productos": 0, "variantes": 0, "price_points": 0}

        prod_rows = [_product_row(p) for p in products]
        var_rows = [r for p in products for r in _variant_rows(p)]
        now = datetime.now(timezone.utc)
        pp_rows = [
            (p.product_uid, v.external_id, now, v.price.amount_cents,
             v.price.compare_at_cents, v.availability.value, v.stock)
            for p in products for v in p.variants
        ]

        set_clause = sql.SQL(", ").join(
            sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c))
            for c in PRODUCT_COLS if c != "product_uid"
        )
        insert_product = sql.SQL("""
            INSERT INTO product ({cols}) VALUES ({vals})
            ON CONFLICT (product_uid) DO UPDATE SET {sets}
        """).format(
            cols=sql.SQL(", ").join(map(sql.Identifier, PRODUCT_COLS)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(PRODUCT_COLS)),
            sets=set_clause,
        )
        insert_variant = sql.SQL("""
            INSERT INTO variant ({cols}) VALUES ({vals})
            ON CONFLICT (product_uid, external_id) DO UPDATE SET {sets}
        """).format(
            cols=sql.SQL(", ").join(map(sql.Identifier, VARIANT_COLS)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(VARIANT_COLS)),
            sets=sql.SQL(", ").join(
                sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c))
                for c in VARIANT_COLS if c not in ("product_uid", "external_id")),
        )

        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(insert_product, prod_rows)
            if var_rows:
                cur.executemany(insert_variant, var_rows)
            if pp_rows:
                cur.executemany("""
                    INSERT INTO price_point (product_uid, variant_external_id,
                        observed_at, price_cents, compare_at_cents, availability, stock)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, pp_rows)

        # sella la corrida: lo que no se tocó en ella quedó obsoleto
        with self.pool.connection() as conn:
            conn.execute("UPDATE store SET last_crawl = %s WHERE slug = %s",
                         (now, products[0].store_slug))

        return {"productos": len(prod_rows), "variantes": len(var_rows),
                "price_points": len(pp_rows)}

    # ------------------------------------------------------------------ embeddings
    def products_without_embedding(self, limit: int = 500) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute("""
                SELECT product_uid, title, description, category, brand,
                       sizes_available, colors_available, seller_name
                FROM product WHERE embedding IS NULL
                ORDER BY product_uid LIMIT %s
            """, (limit,)).fetchall()

    def set_embeddings(self, pairs: Iterable[tuple[str, list[float]]]) -> int:
        rows = [(json.dumps(vec), uid) for uid, vec in pairs]
        if not rows:
            return 0
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE product SET embedding = %s::vector WHERE product_uid = %s", rows)
        return len(rows)

    # ------------------------------------------------------------------ crawls
    def start_run(self, store_slug: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO crawl_run (store_slug) VALUES (%s) RETURNING id",
                (store_slug,)).fetchone()
        return row["id"]

    def finish_run(self, run_id: int, *, discovered: int, parsed_ok: int,
                   failed: int, notes: dict | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute("""
                UPDATE crawl_run SET finished_at = now(), discovered = %s,
                       parsed_ok = %s, failed = %s, notes = %s
                WHERE id = %s
            """, (discovered, parsed_ok, failed, json.dumps(notes or {}), run_id))
