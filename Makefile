CLI := uv run --quiet python -m clickliv

.PHONY: up down logs obs obs-up obs-down obs-logs ping schema load reconcile \
        sessionize occupancy deltas reference verify pipeline all gate-b gate-c \
        sweep chdb marts answers projections scale ui userlevel crossover decline \
        incremental reset

up:
	docker compose up -d --wait

down:
	docker compose down

logs:
	docker compose logs -f clickhouse

obs-up:
	docker compose --profile obs up -d clickstack

obs-down:
	docker compose --profile obs stop clickstack

obs-logs:
	docker compose --profile obs logs -f clickstack

obs:
	$(CLI) obs

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

gate-c:
	uv run --quiet --extra embedded python -m clickliv gate-c

sweep:
	$(CLI) sweep

chdb:
	uv run --quiet --extra embedded python -m clickliv chdb

marts:
	$(CLI) marts

answers:
	$(CLI) answers

projections:
	$(CLI) projections

scale:
	uv run --quiet --extra embedded python -m clickliv scale

ui:
	$(CLI) ui

userlevel:
	$(CLI) userlevel

crossover:
	$(CLI) crossover

decline:
	$(CLI) decline

incremental:
	$(CLI) incremental

reset:
	$(CLI) reset
