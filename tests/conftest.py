import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fadia.adapters.avellaneda import AvellanedaAdapter  # noqa: E402
from fadia.adapters.tiendanube import TiendanubeAdapter  # noqa: E402
from fadia.models import Store  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _store(slug: str, domain: str, platform: str) -> Store:
    return Store(slug=slug, name=slug, domain=domain, platform=platform)


@pytest.fixture
def tiendanube_parse():
    """Parsea HTML congelado sin tocar la red."""
    def _parse(html: str, url: str, slug: str = "sunnyclothing"):
        adapter = TiendanubeAdapter(_store(slug, f"{slug}.ar", "tiendanube"), fetcher=None)
        return asyncio.run(adapter.parse_html(url, html))
    return _parse


@pytest.fixture
def avellaneda_parse():
    def _parse(html: str, url: str):
        adapter = AvellanedaAdapter(
            _store("avellaneda", "www.avellanedaauntoque.com", "avellaneda"), fetcher=None)
        return asyncio.run(adapter.parse_html(url, html))
    return _parse
