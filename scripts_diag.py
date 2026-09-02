"""Diagnóstico de los dos casos raros del banco de pruebas."""
import asyncio
import re
import sys

sys.path.insert(0, "src")
from fadia.adapters.tiendanube import TiendanubeAdapter
from fadia.fetch import Fetcher
from fadia.models import Store


async def diag(domain: str, fetcher: Fetcher, n: int = 4) -> None:
    store = Store(slug=domain.split(".")[0], name=domain, domain=domain, platform="tiendanube")
    ad = TiendanubeAdapter(store, fetcher)
    urls = await ad.discover_product_urls()
    print(f"\n{'='*78}\n{domain}  ({len(urls)} fichas)\n{'='*78}")
    for u in urls[:n]:
        html = await fetcher.get(u)
        raw = ad._extract_variants(html)
        labels = ad._option_labels(html, raw)
        # labels que el HTML declara explicitamente en el form de compra
        form = re.search(r"<form[^>]*js-ajax-cart-panel[^>]*>(.*?)</form>", html, re.S)
        declared = []
        if form:
            declared = [re.sub(r"<[^>]+>", "", m.group(2)).strip().lower()
                        for m in re.finditer(
                            r'<label[^>]*for="variation_(\d+)"[^>]*>(.*?)</label>',
                            form.group(1), re.S)]
        p = await ad.parse_product(u)
        opts = {i: sorted({v.get(f"option{i}") for v in raw if v.get(f"option{i}")})[:5]
                for i in range(3)}
        opts = {k: v for k, v in opts.items() if v}
        print(f"\n  {u.split('/productos/')[-1][:46]}")
        print(f"    variantes={len(raw):4}   labels_declarados={declared or '—'}")
        print(f"    labels_usados={labels}")
        print(f"    valores={opts}")
        if p:
            v0 = p.variants[0] if p.variants else None
            print(f"    -> parse OK  talle={v0.size.raw if v0 and v0.size else None!r:12} "
                  f"color={v0.color.raw if v0 and v0.color else None!r:14} cat={p.category!r}")
        else:
            print("    -> parse devolvio None")


async def main() -> None:
    async with Fetcher(delay=1.0, concurrency=3) as f:
        for d in ("philomenahome.com.ar", "alabamatienda.com.ar", "companiadesombreros.com.ar"):
            await diag(d, f)


asyncio.run(main())
