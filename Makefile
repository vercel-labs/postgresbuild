.PHONY: setup build fix lint typecheck test qa update

setup:
	uv sync
	uv run poe setup

build:
	uv run ggbuild build $(ARGS)

fix:
	uv run poe fix

lint:
	uv run poe lint

typecheck:
	uv run poe typecheck

test:
	uv run poe test

qa:
	uv run poe qa

update:
	uv run ggbuild update
