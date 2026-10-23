PY ?= python3
DB ?= var/geocoder.db
PLACES ?= data/sample/places.csv
ROADS ?= data/sample/roads.geojson
CENTROIDS ?= data/sample/centroids.csv

.PHONY: install test lint data serve eval bench docker

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests scripts

# 用内置区划 + 示例数据建库；接入自有数据时覆盖 PLACES/ROADS/CENTROIDS，另可加 BOUNDARIES
data:
	cngeocoder build --places $(PLACES) --roads $(ROADS) --centroids $(CENTROIDS) \
		$(if $(BOUNDARIES),--boundaries $(BOUNDARIES),) --out $(DB)

serve:
	GEOCODER_DB=$(DB) cngeocoder serve --port 8000

eval:
	GEOCODER_DB=$(DB) cngeocoder eval data/eval/gold_sample.csv --min-district-acc 0.95

bench:
	$(PY) scripts/bench.py --n 1000000 --queries 5000

docker:
	docker build -t geoverse/cngeocoder:0.1.0 .
