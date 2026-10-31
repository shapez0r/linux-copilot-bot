# Python environment lives in .venv (ignored by Git). Override VENV to relocate it.
VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: setup system-deps login run test

$(PY):
	python3 -m venv $(VENV)

# Install Playwright and its Chromium build.
setup: $(PY)
	$(PY) -m pip install --upgrade pip playwright
	$(PY) -m playwright install chromium

# Install the operating-system libraries Chromium needs (requires sudo).
system-deps: $(PY)
	sudo $(PY) -m playwright install-deps chromium

# Sign in to Microsoft 365 Copilot in a visible browser; needs a display.
login: $(PY)
	$(PY) -m bot login

# Start the bot in the foreground.
run: $(PY)
	$(PY) -m bot

test:
	python3 -m unittest discover -s tests -p "test_*.py"
