.PHONY: install contracts check test e2e build dev
PYTHON ?= python
install contracts check test e2e build dev:
	$(PYTHON) scripts/pulse.py $@
