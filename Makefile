PYTHON ?= python3

# Each stack module from this checkout's submodules, else a sibling checkout.
module = $(firstword $(wildcard third_party/$(1)) ../../kilix-modules/$(1))
PANE_DIR ?= $(call module,kilix-pane)
KITTY_INPUT_DIR ?= $(call module,kitty-input)
SOFT_RASTER_DIR ?= $(call module,soft-raster)
PRESENTER_DIR ?= $(call module,kitty-frame-presenter)

TEST_PATH := src:tests:$(PANE_DIR)/src:$(KITTY_INPUT_DIR)/python/src:$(SOFT_RASTER_DIR)/python/src:$(PRESENTER_DIR)/src

.DEFAULT_GOAL := help
.PHONY: help all compile native test e2e run doctor clean

help:
	@printf '%s\n' \
		'kilix-scheduled-task' \
		'' \
		'  make all      byte-compile, and build soft-raster for the pixel window' \
		'  make test     the suite' \
		'  make e2e      against the real systemd user manager (about two minutes)' \
		'  make run      open the window on scratch files under build/, scheduling nothing' \
		'  make doctor   which modules and backend the window gets'

all: compile native

compile:
	$(PYTHON) -m compileall -q src

# The pixel window needs libsoft-raster; without a compiler the window still
# works, drawn with characters.
native:
	@if [ -f third_party/soft-raster/Makefile ]; then \
		$(MAKE) -C third_party/soft-raster all || \
		echo 'kilix-scheduled-task: soft-raster did not build; the window will draw with characters'; \
	fi

test:
	PYTHONPATH=$(TEST_PATH) $(PYTHON) -m unittest discover -s tests

# Creates real units under a unique prefix and removes them afterwards.
e2e:
	KST_E2E=1 PYTHONPATH=$(TEST_PATH) $(PYTHON) -m unittest -v e2e_systemd

run:
	mkdir -p build
	XDG_CONFIG_HOME=$(CURDIR)/build/config XDG_STATE_HOME=$(CURDIR)/build/state XDG_DATA_HOME=$(CURDIR)/build/data KILIX_SCHEDULED_TASK_BACKEND=none PYTHONPATH=$(TEST_PATH) $(PYTHON) -m kilix_scheduled_task

doctor:
	PYTHONPATH=$(TEST_PATH) $(PYTHON) -m kilix_scheduled_task doctor

clean:
	rm -rf build dist src/*.egg-info
	find . -name __pycache__ -prune -exec rm -rf {} +
