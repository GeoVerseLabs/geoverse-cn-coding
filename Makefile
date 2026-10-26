PY ?= python3
DB ?= var/geocoder.db
PLACES ?= data/sample/places.csv
ROADS ?= data/sample/roads.geojson

.PHONY: install test lint data sources serve eval bench docker

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests scripts

# 用内置区划（含中心点、历史别名）+ 示例 POI 建库；接入自有数据时覆盖 PLACES/ROADS，另可加 BOUNDARIES、
# 在线编辑的变更记录 EDITS（cngeocoder edits export 导出）
data:
	cngeocoder build --places $(PLACES) --roads $(ROADS) \
		$(if $(BOUNDARIES),--boundaries $(BOUNDARIES),) $(if $(EDITS),--edits $(EDITS),) --out $(DB)

# 重新生成内置补充数据（需要网络与 pip install -e ".[sources]"）
sources:
	$(PY) scripts/sources/areacodes.py
	$(PY) scripts/sources/overture.py admin

serve:
	GEOCODER_DB=$(DB) cngeocoder serve --port 8000

# 评测卡口：标准地址区县准确率；口语化地址不允许“高置信度但错误”
eval:
	GEOCODER_DB=$(DB) cngeocoder eval data/eval/gold_sample.csv --min-district-acc 0.95 > /dev/null
	GEOCODER_DB=$(DB) cngeocoder eval data/eval/colloquial_sample.csv --min-district-acc 0.95 --max-confident-wrong 0 > /dev/null
	@echo "eval gates passed"

bench:
	$(PY) scripts/bench.py --n 1000000 --queries 5000

docker:
	docker build -t geoverse/cngeocoder:0.1.0 .
