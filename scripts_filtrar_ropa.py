"""Deja en el JSONL de Avellaneda solo lo que es indumentaria.

El marketplace mezcla ropa con blanquería, bazar, electrónica y cosmética:
en el mismo sitemap conviven "Campera de Abrigo" y "Auricular Bluetooth".
Para un buscador de moda, ese ruido es peor que la falta de volumen —
aparece en los resultados y ensucia la búsqueda semántica.

Criterio, en este orden:
  1. Si el clasificador le puso una categoría de indumentaria, entra.
  2. Si el título nombra algo que claramente no es ropa, sale.
  3. El resto entra: la categoría vacía no prueba que no sea ropa —
     "SKORT CONNOR" o "MANGUITAS HONEY" son prendas que el diccionario
     todavía no conoce.
"""
import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")

# Categorías canónicas que SÍ son indumentaria/accesorios de moda
ROPA = {
    "tops", "pants", "jeans", "shorts", "skirts", "dresses", "knitwear",
    "outerwear", "lingerie", "swimwear", "activewear", "shoes", "bags",
    "jewelry", "accessories",
}

# Lo que claramente no lo es. Se aplica solo cuando no hay categoría.
NO_ROPA = re.compile(
    r"\b("
    # blanquería y hogar
    r"sabanas?|sábanas?|acolchado|cubrecama|almohadon|almohadón|almohada|"
    r"toallon|toallón|toallas?|manteles?|cortinas?|repasador|frazada|"
    r"colcha|funda de (?:almohada|sillon|sillón)|alfombra|"
    # bazar y cocina
    r"termo|mate|bombilla|vajilla|tupper|taza|vaso|jarra|olla|sarten|sartén|"
    r"cubiertos|bandeja|organizador|percha|perchas|"
    # electrónica
    r"auricular|auriculares|cargador|cable|parlante|bluetooth|led|foco|"
    r"celular|smartwatch|power ?bank|usb|adaptador|control remoto|velador|"
    # cosmética y cuidado personal
    r"maquillaje|delineador|labial|perfume|sombra|rimel|rímel|esmalte|"
    r"shampoo|acondicionador|crema (?:facial|corporal|de manos)|serum|"
    r"mascarilla|pestañas|cosmetic|brillo labial|base liquida|base líquida|"
    r"polvo (?:compacto|translucido|traslúcido)|corrector|rubor|"
    # papelería, juguetes, herramientas
    r"cuaderno|lapicera|marcador|juguete|peluche|munieco|muñeco|puzzle|"
    r"herramienta|destornillador|linterna|pila|pilas|"
    # varios
    r"planta artificial|maceta|adorno|cuadro decorativo|velas?|sahumerio"
    r")\b", re.I)


def es_ropa(p: dict) -> tuple[bool, str]:
    if p.get("category") in ROPA:
        return True, "categoría"
    titulo = p.get("title") or ""
    if NO_ROPA.search(titulo):
        return False, "título no-ropa"
    return True, "sin categoría, se conserva"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada", type=Path,
                    nargs="?", default=Path("data/avellaneda/products.jsonl"))
    ap.add_argument("--salida", type=Path,
                    default=Path("data/avellaneda/ropa.jsonl"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    motivos = collections.Counter()
    descartados_ej: list[str] = []
    n = 0
    salida = None if a.dry_run else a.salida.open("w", encoding="utf-8")
    try:
        with a.entrada.open(encoding="utf-8") as fh:
            for linea in fh:
                linea = linea.strip()
                if not linea:
                    continue
                n += 1
                try:
                    p = json.loads(linea)
                except json.JSONDecodeError:
                    motivos["json inválido"] += 1
                    continue
                ok, motivo = es_ropa(p)
                motivos[("✓ " if ok else "✗ ") + motivo] += 1
                if ok:
                    if salida:
                        salida.write(linea + "\n")
                elif len(descartados_ej) < 12:
                    descartados_ej.append(p.get("title", "")[:52])
    finally:
        if salida:
            salida.close()

    conservados = sum(v for k, v in motivos.items() if k.startswith("✓"))
    print(f"  leídos      : {n:,}")
    for k, v in motivos.most_common():
        print(f"    {k:32} {v:7,}")
    print(f"\n  conservados : {conservados:,}  ({100*conservados/max(n,1):.1f} %)")
    print(f"  descartados : {n - conservados:,}")
    print("\n  ejemplos descartados:")
    for t in descartados_ej:
        print(f"    {t}")
    if not a.dry_run:
        print(f"\n  -> {a.salida}")


main()
