-- 1. "Qué bajó de precio esta semana" — imposible sin la serie temporal.
SELECT p.title, p.url,
       first_value(pp.price_cents) OVER w / 100.0 AS precio_hoy,
       last_value(pp.price_cents)  OVER w / 100.0 AS precio_hace_7d
FROM price_point pp
JOIN product p USING (product_uid)
WHERE pp.observed_at > now() - interval '7 days'
WINDOW w AS (PARTITION BY pp.product_uid ORDER BY pp.observed_at DESC
             ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING);

-- 2. "Vestido negro talle 38 abajo de 40 lucas, en stock, en cualquier tienda"
--    Un solo join. En un store de documentos: fan-out manual sobre variantes.
SELECT p.title, s.name AS tienda, v.price_cents / 100.0 AS precio, p.url
FROM variant v
JOIN product p USING (product_uid)
JOIN store s ON s.slug = p.store_slug
WHERE p.category = 'dresses'
  AND v.color_normalized = 'black'
  AND v.size_normalized = '38'
  AND v.availability = 'in_stock'
  AND v.price_cents <= 4000000
ORDER BY v.price_cents;

-- 3. La consulta tipo Daydream: intención en lenguaje natural -> embedding,
--    con los filtros duros aplicados en el mismo plan.
SELECT p.title, p.url, p.embedding <=> $1 AS distancia
FROM product p
WHERE p.availability = 'in_stock'
  AND p.gender = 'women'
  AND p.price_min_cents BETWEEN $2 AND $3
ORDER BY p.embedding <=> $1
LIMIT 40;
