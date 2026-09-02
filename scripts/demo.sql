-- Conjunto de demostración: 5 marcas (una por adapter) + una muestra del
-- marketplace mayorista, para que los dos modos de la app tengan datos.
--
-- No es un recorte arbitrario: cada tienda ejercita un adapter distinto,
-- así que si el dump anda, andan los cinco caminos de extracción.
CREATE TEMP TABLE demo_stores(slug text, tope int);
INSERT INTO demo_stores VALUES
  ('sunnyclothing', NULL),   -- tiendanube  · HTML + data-variants
  ('eyelit',        NULL),   -- shopify     · /products.json
  ('47street',      NULL),   -- vtex        · catalog_system
  ('carocuore',     NULL),   -- magento     · GraphQL
  ('markova',       NULL),   -- generico    · JSON-LD
  ('avellaneda',    2000);   -- marketplace · muestra, para el modo Por mayor
