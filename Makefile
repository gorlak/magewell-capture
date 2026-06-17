# Makefile — magewell-capture lifecycle management  (make help for usage)

.DEFAULT_GOAL := status

UDEV_SRC     := packages/magewell/udev/70-magewell.rules
UDEV_DEST    := /etc/udev/rules.d/70-magewell.rules
SERVICE_FILE := /etc/systemd/system/magewell-capture.service
VENV         := .venv/bin/python

.PHONY: help status install run restart clean

# ── help ─────────────────────────────────────────────────────────────────────

help:
	@echo "magewell-capture — lifecycle commands"
	@echo ""
	@echo "  make              show current state (udev rules, venv, service)"
	@echo "  make install      install udev rules + venv + service, then start"
	@echo "  make run          stop service if running; launch interactively"
	@echo "  make restart      restart the running service"
	@echo "  make clean        stop + remove service and udev rules; delete generated files"
	@echo ""
	@echo "States:  CLEAN → (make install) → INSTALL → (make restart) → INSTALL"
	@echo "         CLEAN → (make run)     → RUN"
	@echo "         any   → (make clean)   → CLEAN"

# ── status (default) ──────────────────────────────────────────────────────────

status:
	@printf "udev rules:  "; \
	if [ -f "$(UDEV_DEST)" ]; then echo "installed"; else echo "not installed"; fi
	@printf "virtualenv:  "; \
	if [ -x "$(VENV)" ]; then echo "present"; else echo "not present  →  run: uv sync"; fi
	@printf "service:     "; \
	if [ ! -f "$(SERVICE_FILE)" ]; then \
		echo "not installed"; \
	elif systemctl is-active --quiet magewell-capture 2>/dev/null; then \
		echo "installed, running"; \
	else \
		echo "installed, stopped"; \
	fi

# ── install ───────────────────────────────────────────────────────────────────
# Installs udev rules, virtualenv, and service, then starts it.
# No-op if the service is already running — use 'make restart' to bounce it.

install:
	@if systemctl is-active --quiet magewell-capture 2>/dev/null; then \
		echo "service is already running — nothing to do."; \
		echo "  make restart   restart the running service"; \
		echo "  make run       stop service and run interactively"; \
		exit 0; \
	fi
	@echo "sudo needed — writing to /etc/udev/rules.d and /etc/systemd/system"
	@sudo -v
	@echo "→ udev rules"
	@sudo install -m 0644 -o root -g root $(UDEV_SRC) $(UDEV_DEST)
	@sudo udevadm control --reload
	@sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=2935
	@sudo udevadm trigger --subsystem-match=hidraw
	@echo "→ virtualenv"
	@uv sync --quiet
	@echo "→ systemd service"
	@{ \
		SERVICE_USER="$${SUDO_USER:-$$(logname 2>/dev/null || id -un)}"; \
		echo "[Unit]"; \
		echo "Description=Magewell HDMI Capture Monitor"; \
		echo "After=network.target"; \
		echo ""; \
		echo "[Service]"; \
		echo "Type=simple"; \
		echo "User=$$SERVICE_USER"; \
		echo "WorkingDirectory=$(CURDIR)"; \
		echo "ExecStart=$(CURDIR)/.venv/bin/python $(CURDIR)/scripts/monitor.py -p 80"; \
		echo "Restart=on-failure"; \
		echo "RestartSec=5"; \
		echo "AmbientCapabilities=CAP_NET_BIND_SERVICE"; \
		echo "KillSignal=SIGTERM"; \
		echo "TimeoutStopSec=30"; \
		echo "StandardOutput=journal"; \
		echo "StandardError=journal"; \
		echo "SyslogIdentifier=magewell-capture"; \
		echo ""; \
		echo "[Install]"; \
		echo "WantedBy=multi-user.target"; \
	} | sudo tee $(SERVICE_FILE) > /dev/null
	@sudo chmod 0644 $(SERVICE_FILE)
	@sudo systemctl daemon-reload
	@sudo systemctl enable magewell-capture
	@sudo systemctl start magewell-capture
	@echo "Done. Logs: journalctl -u magewell-capture -f"

# ── run ───────────────────────────────────────────────────────────────────────
# Installs udev rules if missing, stops the service if running, then launches
# directly so you can iterate with Ctrl-C / relaunch.

run:
	@needs_sudo=0; needs_udev=0; needs_stop=0; \
	if [ ! -f "$(UDEV_DEST)" ]; then needs_udev=1; needs_sudo=1; fi; \
	if systemctl is-active --quiet magewell-capture 2>/dev/null; then needs_stop=1; needs_sudo=1; fi; \
	if [ "$$needs_sudo" -eq 1 ]; then \
		printf "sudo needed for:"; \
		[ "$$needs_udev" -eq 1 ] && printf " udev rules"; \
		[ "$$needs_stop" -eq 1 ] && printf " stopping service"; \
		echo ""; \
		sudo -v; \
	fi; \
	if [ "$$needs_udev" -eq 1 ]; then \
		echo "→ installing udev rules"; \
		sudo install -m 0644 -o root -g root $(UDEV_SRC) $(UDEV_DEST); \
		sudo udevadm control --reload; \
		sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=2935; \
		sudo udevadm trigger --subsystem-match=hidraw; \
	fi; \
	if [ ! -x "$(VENV)" ]; then uv sync; fi; \
	if [ "$$needs_stop" -eq 1 ]; then \
		echo "→ stopping service"; \
		sudo systemctl stop magewell-capture; \
	fi; \
	$(VENV) scripts/monitor.py; \
	if [ "$$needs_stop" -eq 1 ]; then \
		echo "Service is stopped. Run 'make install' to restore service mode."; \
	fi

# ── restart ───────────────────────────────────────────────────────────────────
# Intentionally restart the running service. Errors if not running.

restart:
	@if ! systemctl is-active --quiet magewell-capture 2>/dev/null; then \
		echo "error: service is not running — use 'make install' to install and start it" >&2; \
		exit 1; \
	fi
	@echo "sudo needed — restarting magewell-capture"
	@sudo -v
	@sudo systemctl restart magewell-capture
	@echo "Done. Logs: journalctl -u magewell-capture -f"

# ── clean ─────────────────────────────────────────────────────────────────────
# Returns the machine to a clean state: stops and removes the service, removes
# the udev rule, and deletes generated files. Does not remove .venv.

clean:
	@needs_sudo=0; \
	if [ -f "$(SERVICE_FILE)" ] || [ -f "$(UDEV_DEST)" ]; then needs_sudo=1; fi; \
	if [ "$$needs_sudo" -eq 1 ]; then \
		echo "sudo needed — removing /etc/udev/rules.d and /etc/systemd/system entries"; \
		sudo -v; \
	fi; \
	if systemctl is-active --quiet magewell-capture 2>/dev/null; then \
		echo "→ stopping service"; \
		sudo systemctl stop magewell-capture; \
	fi; \
	if systemctl is-enabled --quiet magewell-capture 2>/dev/null; then \
		sudo systemctl disable magewell-capture; \
	fi; \
	if [ -f "$(SERVICE_FILE)" ]; then \
		echo "→ removing service file"; \
		sudo rm -f $(SERVICE_FILE); \
		sudo systemctl daemon-reload; \
	fi; \
	if [ -f "$(UDEV_DEST)" ]; then \
		echo "→ removing udev rules"; \
		sudo rm -f $(UDEV_DEST); \
		sudo udevadm control --reload; \
		sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=2935; \
		sudo udevadm trigger --subsystem-match=hidraw; \
	fi
	@echo "→ removing build artifacts"
	@rm -rf sessions/ .pytest_cache
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Done."
