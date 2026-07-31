PYTHON ?= python3

.PHONY: help lint test preview docs-assets check package release-check

help:
	@printf '%s\n' \
	  'make lint     Ruff 静态检查' \
	  'make test     语法检查和完整 unittest' \
	  'make preview  在 TemporaryDirectory 中预览/执行全部安全演示' \
	  'make docs-assets  重新生成合成 TUI 文档 SVG' \
	  'make check    lint + test + preview' \
	  'make package  构建 wheel 和 sdist（写入 implementation/dist）' \
	  'make release-check  审计已构建归档的内容和 SHA-256'

lint:
	cd implementation && ruff check openclean tests scripts

test:
	cd implementation && $(PYTHON) -W error -m py_compile openclean/*.py tests/*.py scripts/*.py
	cd implementation && PYTHONPATH=. $(PYTHON) scripts/capture_tui_assets.py --check
	cd implementation && PYTHONPATH=. $(PYTHON) -W error -m unittest discover -s tests -q

preview:
	cd implementation && PYTHONPATH=. $(PYTHON) scripts/preview_all.py

docs-assets:
	cd implementation && PYTHONPATH=. $(PYTHON) scripts/capture_tui_assets.py --write

check: lint test preview

package:
	cd implementation && $(PYTHON) -m build --no-isolation

release-check:
	cd implementation && $(PYTHON) scripts/check_release_artifacts.py
