PYTHON ?= python3
RUFF ?= ruff

.PHONY: help lint test test-focused preview docs-assets check ci-check package release-check

help:
	@printf '%s\n' \
	  'make lint     Ruff 静态检查' \
	  'make test     语法检查和完整 unittest' \
	  'make preview  在 TemporaryDirectory 中预览/执行全部安全演示' \
	  'make docs-assets  重新生成合成 TUI 文档 SVG' \
	  'make check    本地轻量语法和 CLI 启动检查（无需开发依赖）' \
	  'make test-focused TEST_PATTERN=test_agent_identifiers.py  仅运行指定测试文件' \
	  'make ci-check 云端全量 lint + test + preview；本地按需复现' \
	  'make package  构建 wheel 和 sdist（写入 implementation/dist）' \
	  'make release-check  审计已构建归档的内容和 SHA-256'

lint:
	cd implementation && $(RUFF) check openclean tests scripts

check:
	cd implementation && $(PYTHON) -W error -m py_compile openclean/*.py openclean/*/*.py
	cd implementation && PYTHONPATH=. $(PYTHON) -m openclean --version
	cd implementation && PYTHONPATH=. $(PYTHON) -m openclean --help > /dev/null

test-focused:
	@test -n "$(TEST_PATTERN)" || { printf '%s\n' '请指定 TEST_PATTERN，例如 test_agent_identifiers.py'; exit 2; }
	cd implementation && PYTHONPATH=. $(PYTHON) -W error scripts/run_focused_tests.py '$(TEST_PATTERN)'

test:
	cd implementation && $(PYTHON) -W error -m py_compile openclean/*.py openclean/*/*.py tests/*.py scripts/*.py
	cd implementation && PYTHONPATH=. $(PYTHON) scripts/capture_tui_assets.py --check
	cd implementation && PYTHONPATH=. $(PYTHON) -W error -m unittest discover -s tests -q

preview:
	cd implementation && PYTHONPATH=. $(PYTHON) scripts/preview_all.py

docs-assets:
	cd implementation && PYTHONPATH=. $(PYTHON) scripts/capture_tui_assets.py --write

ci-check: lint test preview

package:
	cd implementation && $(PYTHON) -m build --no-isolation

release-check:
	cd implementation && $(PYTHON) scripts/check_release_artifacts.py
