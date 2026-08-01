CLI := uv run --quiet python -m clickliv

.PHONY: up down logs obs obs-up obs-down obs-logs ping schema load reconcile \
        sessionize occupancy deltas reference verify pipeline all gate-b gate-c \
        sweep chdb marts answers projections scale ui userlevel crossover decline \
        incremental instantaneous submission replay unseen unseen-fixture unseen-variants \
        preflight rollback mcp test data fixture fixture-pipeline reset \
        llm-up llm-down llm-logs chat-up chat-down chat-logs

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

instantaneous:
	$(CLI) instantaneous

submission:
	$(CLI) submission

replay:
	$(CLI) replay

# The sealed-dataset run. See docs/unseen-day.md.
unseen:
	@test -n "$(RAW)" -a -n "$(CONTENT)" || { \
	  echo "usage: make unseen RAW=<events csv> CONTENT=<content csv> [OUT=unseen] [DB=<database>] [CSV_RENAME=theirs=ours,...]"; \
	  exit 2; }
	RAW_CSV="$(RAW)" CONTENT_CSV="$(CONTENT)" \
	UNSEEN_DIR="$(if $(OUT),$(OUT),unseen)" \
	$(if $(CSV_RENAME),CSV_RENAME="$(CSV_RENAME)",) \
	$(if $(DB),CH_DATABASE="$(DB)",) $(CLI) unseen

# Read only. Everything checkable about a new pair of files, before anything is dropped.
preflight:
	@test -n "$(RAW)" -a -n "$(CONTENT)" || { \
	  echo "usage: make preflight RAW=<events csv> CONTENT=<content csv> [CSV_RENAME=theirs=ours,...]"; \
	  exit 2; }
	RAW_CSV="$(RAW)" CONTENT_CSV="$(CONTENT)" \
	$(if $(CSV_RENAME),CSV_RENAME="$(CSV_RENAME)",) \
	$(if $(DB),CH_DATABASE="$(DB)",) $(CLI) preflight

rollback:
	$(if $(DB),CH_DATABASE="$(DB)",) $(CLI) rollback

unseen-fixture:
	uv run --quiet python tools/make_unseen_fixture.py

# The same fresh day in every container and CSV quirk the organizers might ship.
unseen-variants:
	uv run --quiet python tools/make_unseen_fixture.py --variants $(if $(DIR),$(DIR),fixtures/variants)

mcp:
	$(CLI) mcp

llm-up:
	docker compose --profile llm up -d

llm-down:
	docker compose --profile llm stop

llm-logs:
	docker compose --profile llm logs -f langfuse-web langfuse-worker

chat-up:
	docker compose --profile chat up -d

chat-down:
	docker compose --profile chat stop

chat-logs:
	docker compose --profile chat logs -f librechat

test:
	uv run --quiet python -m unittest discover -s tests -v

data:
	uv run --quiet python tools/fetch_data.py

fixture:
	uv run --quiet python tools/make_fixture.py

fixture-pipeline:
	uv run --quiet --extra embedded python -m unittest tests.test_fixture_pipeline -v

reset:
	$(CLI) reset
