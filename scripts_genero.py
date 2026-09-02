"""Completa el género de los productos que quedaron sin clasificar.

Dos señales, aplicadas en orden y solo cuando son fuertes:

1. **Categoría.** Vestidos, polleras y lencería son, en los catálogos
   argentinos relevados, casi siempre de la línea de mujer.
2. **Marca dominante.** Si de lo que SÍ se clasificó en una tienda el 90 %
   o más cae de un lado, la tienda es de esa línea y el resto hereda. Ayres
   y Las Pepas no dicen "mujer" en ningún breadcrumb —dicen "indumentaria"—
   pero todo su catálogo lo es.

Lo que no alcanza el umbral queda en `unknown` a propósito: es mejor un
filtro que dice "sin género" que uno que asigna mal y el usuario no puede
corregir.
"""
import argparse
import sys

sys.path.insert(0, "src")
from fadia.storage.repository import Repository

DSN = "postgresql://fadia:fadia@localhost:5433/fadia"
UMBRAL = 0.90        # dominancia mínima para heredar
MINIMO = 25          # productos ya clasificados para que la marca cuente


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--umbral", type=float, default=UMBRAL)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    repo = Repository(a.dsn)
    try:
        with repo.pool.connection() as conn:
            antes = conn.execute("""
                SELECT count(*) FILTER (WHERE gender NOT IN ('women','men','kids')) AS sin,
                       count(*) AS total FROM product
            """).fetchone()
            print(f"  sin género antes: {antes['sin']:,} de {antes['total']:,}")

            # --- 1) por categoría
            sql_cat = """
                UPDATE product SET gender = 'women'
                WHERE gender NOT IN ('women','men','kids')
                  AND category IN ('dresses','skirts','lingerie')
            """
            n1 = conn.execute(sql_cat + (" AND false" if a.dry_run else "")).rowcount
            if a.dry_run:
                n1 = conn.execute("""
                    SELECT count(*) FROM product
                    WHERE gender NOT IN ('women','men','kids')
                      AND category IN ('dresses','skirts','lingerie')""").fetchone()["count"]
            print(f"  por categoría   : {n1:,}")

            # --- 2) marca dominante
            dominantes = conn.execute("""
                SELECT store_slug,
                       mode() WITHIN GROUP (ORDER BY gender) AS dominante,
                       count(*) AS clasificados,
                       max(share) AS share
                FROM (
                  SELECT store_slug, gender,
                         count(*) OVER (PARTITION BY store_slug, gender)::float
                         / count(*) OVER (PARTITION BY store_slug) AS share
                  FROM product WHERE gender IN ('women','men','kids')
                ) t
                GROUP BY store_slug
                HAVING count(*) >= %s AND max(share) >= %s
            """, (MINIMO, a.umbral)).fetchall()

            n2 = 0
            for d in dominantes:
                if a.dry_run:
                    c = conn.execute("""
                        SELECT count(*) FROM product
                        WHERE store_slug = %s AND gender NOT IN ('women','men','kids')
                    """, (d["store_slug"],)).fetchone()["count"]
                else:
                    c = conn.execute("""
                        UPDATE product SET gender = %s
                        WHERE store_slug = %s AND gender NOT IN ('women','men','kids')
                    """, (d["dominante"], d["store_slug"])).rowcount
                if c:
                    print(f"    {d['store_slug'][:18]:20} -> {d['dominante']:6} "
                          f"({d['share']:.0%} de {d['clasificados']}) · {c:,} productos")
                n2 += c
            print(f"  por marca       : {n2:,}")

            despues = conn.execute("""
                SELECT count(*) FILTER (WHERE gender NOT IN ('women','men','kids')) AS sin,
                       count(*) AS total FROM product
            """).fetchone()
            print(f"\n  sin género {'quedaría' if a.dry_run else 'ahora'}: "
                  f"{despues['sin']:,} de {despues['total']:,} "
                  f"({100*despues['sin']/despues['total']:.1f} %)")
    finally:
        repo.close()


main()
