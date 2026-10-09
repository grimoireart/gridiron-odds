# Gridiron Odds — D15 commands. Stdlib Python only (3.11+); the interpreter is `python3`.
#   make build   fetch -> rate -> predict -> sim -> ledger -> model.json -> index.html  (< 30 s)
#   make tune    grid search -> params.json (prints the holdout table)
#   make test    unit tests + Elo reference check
#   make serve   static server on http://localhost:8787
# Env: GRIDIRON_OFFLINE=1 uses data/games.csv without fetching; ODDS_API_KEY enables the official odds snapshot.

PY ?= python3
PORT ?= 8787

.PHONY: build tune test serve clean

build:
	$(PY) -m src.build

tune:
	$(PY) -m src.tune

test:
	$(PY) -m unittest discover -s tests -v
	GRIDIRON_OFFLINE=1 $(PY) -m src.evaluate --reference-check

serve:
	$(PY) -m http.server $(PORT)

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -f data/*.tmp
