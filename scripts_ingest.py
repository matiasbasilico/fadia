"""Ingesta un JSONL de productos a Postgres.

  uv run python scripts_ingest.py data/sunny_full.jsonl --store sunnyclothing
  uv run python scripts_ingest.py data/avellaneda/products.jsonl --store avellaneda

Valida antes de escribir: si el arnés encuentra algo BLOQUEANTE, no ingiere.
Es el punto donde el arnés deja de ser un informe y se vuelve un portón.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from fadia.models import Product, Store
from fadia.storage.repository import Repository
from fadia.validate import Baseline, Severity, coverage_report, validate

DSN = "postgresql://fadia:fadia@localhost:5433/fadia"

STORES = {
    "sunnyclothing": Store(slug="sunnyclothing", name="SUNNY.",
                           domain="www.sunnyclothing.ar", platform="tiendanube"),
    "avellaneda": Store(slug="avellaneda", name="Avellaneda a un Toque",
                        domain="www.avellanedaauntoque.com", platform="avellaneda"),
    "eyelit": Store(slug="eyelit", name="Eyelit",
                    domain="eyelit.com.ar", platform="shopify"),
    "47street": Store(slug="47street", name="47 Street",
                      domain="47street.com.ar", platform="vtex"),
    "carocuore": Store(slug="carocuore", name="Caro Cuore",
                       domain="carocuore.com.ar", platform="magento"),
}


def batched(it, n):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--store", required=True, choices=list(STORES))
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--force", action="store_true",
                    help="ingerir aunque el arnés bloquee")
    a = ap.parse_args()

    print(f"[cargar] {a.path}")
    products = [Product.model_validate_json(l) for l in
                a.path.open(encoding="utf-8") if l.strip()]
    print(f"[cargar] {len(products)} productos")

    base = None
    if a.baseline and a.baseline.exists():
        base = Baseline(**json.loads(a.baseline.read_text(encoding="utf-8")))

    findings = validate(products, baseline=base)
    blocking = [f for f in findings if f.severity is Severity.BLOCK]
    for f in findings:
        print(f"[arnes] {f.severity.value.upper():5} {f.check}: {f.message}")
    if blocking and not a.force:
        print(f"\n[arnes] {len(blocking)} hallazgos BLOQUEANTES. No se ingiere. "
              f"Usá --force para forzar.")
        return 2

    repo = Repository(a.dsn)
    try:
        store = STORES[a.store]
        repo.upsert_store(store)
        run_id = repo.start_run(store.slug)

        # solo lo que cambió: el content_hash evita reescribir el catálogo entero
        known = repo.existing_hashes(store.slug)
        nuevos = [p for p in products
                  if known.get(p.product_uid) != p.content_hash]
        print(f"[upsert] {len(nuevos)} con cambios · "
              f"{len(products) - len(nuevos)} intactos (se saltean)")

        total = {"productos": 0, "variantes": 0, "price_points": 0}
        for i, chunk in enumerate(batched(nuevos, a.batch), 1):
            counts = repo.upsert_products(chunk)
            for k, v in counts.items():
                total[k] += v
            print(f"  lote {i:>3}  {total['productos']:>7} productos  "
                  f"{total['variantes']:>7} variantes  "
                  f"{total['price_points']:>7} price_points", flush=True)

        repo.finish_run(run_id, discovered=len(products),
                        parsed_ok=total["productos"],
                        failed=len(products) - len(nuevos),
                        notes={"coverage": coverage_report(products),
                               "findings": [f.check for f in findings]})
        print(f"\n[listo] {total}")
    finally:
        repo.close()
    return 0


raise SystemExit(main())
