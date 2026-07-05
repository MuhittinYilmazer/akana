.PHONY: help setup setup-y start stop doctor smoke test ship reset-memory

# Every target proxies to `python akana.py <cmd>`. The primary interface is `akana.py`.
PY ?= python3

help:
	@echo "Akana — primary interface: python akana.py <cmd>"
	@echo
	@echo "Shortcuts:"
	@echo "  make setup        Interactive install"
	@echo "  make setup-y      Unattended install (CI / voice=none)"
	@echo "  make start        Start the server"
	@echo "  make stop         Stop the server"
	@echo "  make doctor       Pre-flight checks"
	@echo "  make smoke        Quick smoke test"
	@echo "  make test         Full test suite"
	@echo "  make ship         Pack a portable tarball"
	@echo "  make reset-memory Reset Inbox / staging / semantic / graph"

setup:
	$(PY) akana.py setup

setup-y:
	$(PY) akana.py setup -y

start:
	$(PY) akana.py start

stop:
	$(PY) akana.py stop

doctor:
	$(PY) akana.py doctor

smoke:
	$(PY) akana.py smoke

test:
	$(PY) akana.py test

ship:
	$(PY) akana.py ship

reset-memory:
	$(PY) akana.py reset-memory
