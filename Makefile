# FadIA — buscador conversacional de moda argentina
.DEFAULT_GOAL := ayuda
SHELL := /bin/bash

ayuda:  ## muestra esta ayuda
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-12s\033[0m %s\n",$$1,$$2}'

up:        ## levanta base + api + web  (http://localhost:5173)
	docker compose up -d --build
	@echo "  web  → http://localhost:5173"
	@echo "  api  → http://localhost:8080/health"

down:      ## detiene el stack (conserva los datos)
	docker compose down

logs:      ## sigue los logs de la api
	docker compose logs -f api

modelo:    ## deja LM Studio listo (los 2 modelos residentes)
	./scripts_lms_setup.sh

restore:   ## carga el dump de demostración, con embeddings ya calculados
	./scripts/restore.sh

dump:      ## exporta el conjunto de demostración a data/fadia-demo.sql.gz
	./scripts/dump.sh

seed:      ## scrapea las 5 tiendas desde cero (~15 min, necesita el modelo)
	./scripts/seed.sh

reset:     ## BORRA la base y la deja vacía (ofrece dump antes)
	./scripts/reset.sh

test:      ## corre los 48 tests de regresión
	uv run pytest -q

estado:    ## qué hay cargado ahora mismo
	@docker compose exec -T db psql -U fadia -d fadia -c "\
	  SELECT store_slug AS tienda, count(*) AS productos, \
	         count(*) FILTER (WHERE embedding IS NOT NULL) AS indexados \
	  FROM product GROUP BY 1 ORDER BY 2 DESC;"

.PHONY: ayuda up down logs modelo restore dump seed reset test estado
