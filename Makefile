.PHONY: install test lint security check web
install:
	pip install -e ".[dev]"
test:
	python -m pytest
lint:
	python -m pyflakes eventmate tests
	node --check eventmate/web/static/app.js
	node --check eventmate/web/static/arcade.js
	node --check eventmate/web/static/arcade3d.js
security:
	python -m bandit -q -r eventmate -ll
check: lint security test
web:
	python -m eventmate.web
