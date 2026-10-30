.PHONY: install test lint web
install:
	pip install -e ".[dev]"
test:
	python -m pytest
lint:
	python -m pyflakes eventmate tests
web:
	python -m eventmate.web
