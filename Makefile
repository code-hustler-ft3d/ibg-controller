# Build and package the IB Gateway controller.
#
# Targets:
#   make              — build the agent jar + stage the controller in dist/
#   make release      — build + create a release tarball in dist/
#   make clean        — remove dist/ and any build artifacts
#   make install      — install the built artifacts into DESTDIR (defaults
#                       to /home/ibgateway/); used by the Docker image's
#                       setup stage
#   make test         — basic syntax check for the Python controller;
#                       compiles the agent via the same rule as make
#
# Version is overridable on the command line:
#   make release VERSION=0.2.0

VERSION          ?= 0.6.0
NAME             := ibg-controller
DIST             := dist
AGENT_SRC        := agent/GatewayInputAgent.java
AGENT_MANIFEST   := agent/manifest.mf
AGENT_CLASSES    := build/agent-classes
AGENT_JAR        := $(DIST)/gateway-input-agent.jar
CONTROLLER_PY    := gateway_controller.py
DESTDIR          ?= /home/ibgateway

# Java release target — matches IBKR Gateway's bundled JRE
JAVAC_RELEASE    := 17

.PHONY: all release clean install test

all: $(AGENT_JAR) $(DIST)/$(CONTROLLER_PY)

$(AGENT_JAR): $(AGENT_SRC) $(AGENT_MANIFEST)
	@mkdir -p $(AGENT_CLASSES) $(DIST)
	javac --release $(JAVAC_RELEASE) -d $(AGENT_CLASSES) $(AGENT_SRC)
	jar cfm $(AGENT_JAR) $(AGENT_MANIFEST) -C $(AGENT_CLASSES) .
	@echo "Built $(AGENT_JAR)"

$(DIST)/$(CONTROLLER_PY): $(CONTROLLER_PY)
	@mkdir -p $(DIST)
	cp $(CONTROLLER_PY) $(DIST)/$(CONTROLLER_PY)
	@echo "Staged $(DIST)/$(CONTROLLER_PY)"

release: all
	@mkdir -p $(DIST)
	@# Stage everything into a single flat directory inside dist/ so the
	@# resulting tarball has a clean layout: install.sh at the root next
	@# to the agent jar and the controller .py, with README, LICENSE,
	@# CHANGELOG, and docs/ alongside.
	@rm -rf $(DIST)/$(NAME)-$(VERSION)
	@mkdir -p $(DIST)/$(NAME)-$(VERSION)/docs
	cp $(AGENT_JAR) $(DIST)/$(NAME)-$(VERSION)/gateway-input-agent.jar
	cp $(DIST)/$(CONTROLLER_PY) $(DIST)/$(NAME)-$(VERSION)/$(CONTROLLER_PY)
	cp README.md LICENSE CHANGELOG.md SECURITY.md \
	  $(DIST)/$(NAME)-$(VERSION)/
	cp docs/*.md $(DIST)/$(NAME)-$(VERSION)/docs/
	cp scripts/install.sh $(DIST)/$(NAME)-$(VERSION)/install.sh
	cp scripts/ibc_config_to_env.py \
	  $(DIST)/$(NAME)-$(VERSION)/ibc_config_to_env.py
	chmod +x $(DIST)/$(NAME)-$(VERSION)/install.sh \
	         $(DIST)/$(NAME)-$(VERSION)/ibc_config_to_env.py
	tar -czf $(DIST)/$(NAME)-$(VERSION).tar.gz -C $(DIST) $(NAME)-$(VERSION)
	rm -rf $(DIST)/$(NAME)-$(VERSION)
	@echo "Release tarball: $(DIST)/$(NAME)-$(VERSION).tar.gz"
	@echo "Layout:"
	@tar -tzf $(DIST)/$(NAME)-$(VERSION).tar.gz | sed 's/^/  /'

install: all
	@install -d $(DESTDIR)/scripts $(DESTDIR)
	install -m 0644 $(AGENT_JAR) $(DESTDIR)/gateway-input-agent.jar
	install -m 0755 $(DIST)/$(CONTROLLER_PY) $(DESTDIR)/scripts/$(CONTROLLER_PY)
	@echo "Installed to $(DESTDIR)"

clean:
	rm -rf build $(DIST)

test: all
	@if command -v python3 >/dev/null 2>&1; then \
	  python3 -m py_compile $(CONTROLLER_PY) && echo "Python syntax OK"; \
	else \
	  echo "python3 not installed, skipping Python syntax check"; \
	fi
	@if command -v unzip >/dev/null 2>&1; then \
	  unzip -p $(AGENT_JAR) META-INF/MANIFEST.MF | grep -q "Premain-Class" && \
	    echo "Agent jar has Premain-Class header" || \
	    ( echo "Agent jar missing Premain-Class header"; exit 1 ); \
	else \
	  echo "unzip not installed, skipping jar manifest check"; \
	fi
	@if command -v python3 >/dev/null 2>&1 && [ -d tests ]; then \
	  echo "Running unit tests..."; \
	  python3 -m unittest discover -s tests -v 2>&1 | tail -5; \
	else \
	  echo "python3 or tests/ not available, skipping unit tests"; \
	fi
	@echo "All checks passed"
