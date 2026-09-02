"""Valida un JSONL de productos contra el arnés.

  uv run python scripts_validate.py data/sunny_full.jsonl
  uv run python scripts_validate.py data/avellaneda/products.jsonl --expected 139044
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from fadia.models import Product
from fadia.validate import Baseline, Severity, coverage_report, validate

ICON = {Severity.BLOCK: "BLOQUEA", Severity.WARN: "AVISA  ", Severity.INFO: "INFO   "}


def load(path: Path) -> list[Product]:
    out = []
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(Product.model_validate_json(line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--expected", type=int, default=None)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--save-baseline", type=Path, default=None)
    a = ap.parse_args()

    products = load(a.path)
    base = None
    if a.baseline and a.baseline.exists():
        base = Baseline(**json.loads(a.baseline.read_text(encoding="utf-8")))

    findings = validate(products, baseline=base, expected_urls=a.expected)

    print(f"\n{a.path}  —  {len(products)} productos")
    print("=" * 74)
    if not findings:
        print("  sin hallazgos: el lote pasa.")
    for f in findings:
        print(f"  [{ICON[f.severity]}] {f.check}")
        print(f"            {f.message}")
        for s in f.sample:
            print(f"            · {s[:88]}")

    print("\n  cobertura:")
    for k, v in coverage_report(products).items():
        bar = "#" * round(24 * v)
        print(f"    {k:12} {v:6.1%}  {bar}")

    if a.save_baseline:
        import statistics as st
        cents = [p.price_min.amount_cents for p in products if p.price_min.amount_cents > 0]
        a.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        a.save_baseline.write_text(json.dumps({
            "n_products": len(products),
            "median_price_cents": int(st.median(cents)) if cents else None,
            "coverage": coverage_report(products),
        }, indent=1), encoding="utf-8")
        print(f"\n  baseline guardado -> {a.save_baseline}")

    blocking = [f for f in findings if f.severity is Severity.BLOCK]
    print(f"\n  veredicto: {'BLOQUEADO' if blocking else 'PASA'}"
          f"  ({len(blocking)} bloqueantes, "
          f"{sum(1 for f in findings if f.severity is Severity.WARN)} avisos)")
    return 1 if blocking else 0


raise SystemExit(main())
