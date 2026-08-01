CLI := uv run --quiet python -m clickliv

.PHONY: up down logs ping schema load reconcile sessionize occupancy deltas \
        reference verify pipeline all gate-b sweep chdb reset

up:
	docker compose up -d --wait

down:
	docker compose down

logs:
	docker compose logs -f clickhouse

ping:
	$(CLI) ping

schema:
	$(CLI) schema

load:
	$(CLI) load

reconcile:
	$(CLI) reconcile

sessionize:
	$(CLI) sessionize

occupancy:
	$(CLI) occupancy

deltas:
	$(CLI) deltas

reference:
	$(CLI) reference

verify:
	$(CLI) verify

pipeline:
	$(CLI) pipeline

all:
	$(CLI) all

gate-b:
	$(CLI) gate-b

sweep:
	$(CLI) sweep

chdb:
	uv run --quiet --extra embedded python -m clickliv chdb

reset:
	$(CLI) reset
