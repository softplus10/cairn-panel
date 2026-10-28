PYTHON ?= python3

.PHONY: demo test daemon check

demo:
	$(PYTHON) -m cwm_panel --demo --open settings

test:
	$(PYTHON) -m unittest discover -s tests -v
	cd daemon && cargo test

daemon:
	cd daemon && cargo build --release

check:
	$(PYTHON) -m compileall -q cwm_panel
	cd daemon && cargo fmt --check
	cd daemon && cargo clippy --all-targets -- -D warnings
