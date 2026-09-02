"""Cuenta qué hay realmente adentro de un dump comprimido.

No sirve preguntarle a la base cuántas filas tiene la tabla: la muestra
del marketplace está topeada y el conteo directo exageraba por 137.000.
Lo único que dice la verdad es contar las filas del propio archivo.
"""
import gzip
import sys
from pathlib import Path


def contar(path: Path) -> dict[str, int]:
    tablas: dict[str, int] = {}
    actual, n = None, 0
    abrir = gzip.open if path.suffix == ".gz" else open
    with abrir(path, "rt", encoding="utf-8", errors="replace") as fh:
        for linea in fh:
            if linea.startswith("COPY "):
                actual = linea.split()[1].split("(")[0]
                n = 0
            elif linea.startswith("\\.") and actual:
                tablas[actual] = n
                actual = None
            elif actual:
                n += 1
    return tablas


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/fadia-demo.sql.gz")
    if not path.exists():
        print(f"  no existe {path}")
        return 1
    mb = path.stat().st_size / 1_048_576
    print(f"  {path}  ·  {mb:.1f} MB")
    tablas = contar(path)
    if not tablas:
        print("  el dump no tiene filas (¿se generó vacío?)")
        return 1
    for t, n in sorted(tablas.items(), key=lambda kv: -kv[1]):
        print(f"    {t:24} {n:>7,} filas")
    return 0


raise SystemExit(main())
