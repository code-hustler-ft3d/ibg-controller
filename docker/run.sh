#!/bin/bash
# shellcheck disable=SC2317
# Don't warn about unreachable commands in this file

set -Eeo pipefail

echo "*************************************************************************"
echo ".> Starting IBC/IB gateway"
echo "*************************************************************************"

# shellcheck disable=SC1091
source "${SCRIPT_PATH}/common.sh"

# Backward compatibility: USE_PYATSPI2_CONTROLLER is the historical name
# for the controller toggle (the controller used to walk the AT-SPI
# desktop tree via pyatspi). v0.5.12+ no longer registers Gateway with
# AT-SPI at all; the env var is now just the IBC-vs-controller switch.
# USE_IBG_CONTROLLER is the preferred name. Honor the old name for
# existing compose files but warn so users migrate.
if [ -z "${USE_IBG_CONTROLLER:-}" ] && [ -n "${USE_PYATSPI2_CONTROLLER:-}" ]; then
	USE_IBG_CONTROLLER="$USE_PYATSPI2_CONTROLLER"
	echo ".> WARNING: USE_PYATSPI2_CONTROLLER is deprecated; rename to USE_IBG_CONTROLLER"
fi
export USE_IBG_CONTROLLER

# shellcheck disable=SC2329
stop_ibc() {
	echo ".> 😘 Received SIGINT or SIGTERM. Shutting down IB Gateway."

	# 2026-04-27 fix: SIGTERM the controllers FIRST so they can drive
	# clean logout via the input agent BEFORE we tear down the X server
	# / socat infrastructure they depend on. The old order (Xvfb killed
	# first, controllers signalled last) made the v0.5.6 clean-logout
	# pipeline impossible: AWT's WINDOW_CLOSING dispatch needs a live
	# X11 connection to fire Gateway's WindowListener, so by the time
	# the controller's shutdown handler tried clean logout, AWT
	# EventQueue was blocked on a dead X11 socket and the JVM hung
	# until SIGKILL — stranding the IBKR slot every container restart.
	echo ".> Stopping IBC / controller (clean logout phase)."
	kill -SIGTERM "${pid[@]}" 2>/dev/null || true
	# Wait up to 60s for controllers to do clean logout + JVM exit.
	# Per-controller _CLEAN_LOGOUT_TIMEOUT_SECONDS defaults to 15s,
	# plus JVM shutdown-hook grace; allow margin for both modes.
	for _i in $(seq 1 60); do
		_all_done=true
		for _p in "${pid[@]}"; do
			if kill -0 "$_p" 2>/dev/null; then
				_all_done=false
				break
			fi
		done
		$_all_done && break
		sleep 1
	done
	if ! $_all_done; then
		echo ".> Controllers did not exit within 60s; proceeding to teardown anyway."
	fi

	#
	if pgrep x11vnc >/dev/null; then
		echo ".> Stopping x11vnc."
		pkill x11vnc
	fi
	#
	echo ".> Stopping Xvfb."
	pkill Xvfb
	#
	if [ -n "$SSH_TUNNEL" ]; then
		echo ".> Stopping ssh."
		pkill run_ssh.sh
		pkill ssh
		echo ".> Stopping socat."
		pkill run_socat.sh
		pkill socat
	else
		echo ".> Stopping socat."
		pkill run_socat.sh
		pkill socat
	fi
	#
	if [ "$USE_IBG_CONTROLLER" = "yes" ]; then
		echo ".> Stopping window manager."
		pkill -f matchbox-window-manager 2>/dev/null
		# Clean up readiness file
		rm -f /tmp/gateway_ready
	fi
	# All done.
	echo ".> Done... $?"
}

start_xvfb() {
	# start Xvfb
	echo ".> Starting Xvfb server"
	DISPLAY=:1
	export DISPLAY
	rm -f /tmp/.X1-lock
	Xvfb $DISPLAY -ac -screen 0 1024x768x16 &
}

start_vnc() {
	# wait for X11 socket to be ready
	wait_x_socket
	# start VNC server
	file_env 'VNC_SERVER_PASSWORD'
	if [ -n "$VNC_SERVER_PASSWORD" ]; then
		echo ".> Starting VNC server"
		x11vnc -ncache_cr -display $DISPLAY -forever -shared -bg -noipv6 \
			-passwd "$VNC_SERVER_PASSWORD" &
		unset_env 'VNC_SERVER_PASSWORD'
	else
		echo ".> VNC server disabled"
	fi
}

start_IBC() {
	echo ".> Starting IBC in ${TRADING_MODE} mode, with params:"
	echo ".>		Version: ${TWS_MAJOR_VRSN}"
	echo ".>		program: ${IBC_COMMAND:-gateway}"
	echo ".>		tws-path: ${TWS_PATH}"
	echo ".>		ibc-path: ${IBC_PATH}"
	echo ".>		ibc-init: ${IBC_INI}"
	echo ".>		tws-settings-path: ${TWS_SETTINGS_PATH:-$TWS_PATH}"
	echo ".>		on2fatimeout: ${TWOFA_TIMEOUT_ACTION}"
	# start IBC -g for gateway
	"${IBC_PATH}/scripts/ibcstart.sh" "${TWS_MAJOR_VRSN}" -g \
		"--tws-path=${TWS_PATH}" \
		"--ibc-path=${IBC_PATH}" "--ibc-ini=${IBC_INI}" \
		"--on2fatimeout=${TWOFA_TIMEOUT_ACTION}" \
		"--tws-settings-path=${TWS_SETTINGS_PATH:-}" &
	_p="$!"
	pid+=("$_p")
	export pid
	echo "$_p" >"/tmp/pid_${TRADING_MODE}"
}

# ── ibg-controller path ─────────────────────────────────────────────────
# Opt-in via USE_IBG_CONTROLLER=yes (historically USE_PYATSPI2_CONTROLLER;
# alias handled at the top of this script). Default falls back to IBC.

start_window_manager() {
	# Xvfb has no window manager by default, which leaves no focused
	# window for synthetic input handling. Matchbox is a tiny WM that
	# manages focus without adding decorations. Without --use_titlebar no,
	# Gateway windows would get a duplicate title bar.
	echo ".> Starting matchbox window manager."
	matchbox-window-manager -use_titlebar no >/dev/null 2>&1 &
	sleep 1
}

start_controller() {
	echo ".> Starting Gateway controller in ${TRADING_MODE} mode."
	echo ".>		Version: ${TWS_MAJOR_VRSN}"
	echo ".>		tws-path: ${TWS_PATH}"
	echo ".>		tws-settings-path: ${TWS_SETTINGS_PATH:-$TWS_PATH}"

	# Dual-mode support: each instance needs its own agent Unix socket,
	# its own readiness file, and its own jtsConfigDir so the two
	# live/paper controllers don't trample each other. Single-mode falls
	# back to the defaults.
	export GATEWAY_INPUT_AGENT_SOCKET="/tmp/gateway-input-${TRADING_MODE}.sock"
	export CONTROLLER_READY_FILE="/tmp/gateway_ready_${TRADING_MODE}"
	# TWS_SETTINGS_PATH gets assigned in the outer dual-mode dispatch
	# block but may not have been exported there (the existing IBC path
	# accessed it as a shell variable, not an env var). Force-export
	# here so the Python controller subprocess actually sees it and
	# routes its jts.ini / state to the per-instance directory.
	if [ -n "$TWS_SETTINGS_PATH" ]; then
		export TWS_SETTINGS_PATH
	fi

	# Dual-mode command server port offset. If CONTROLLER_COMMAND_SERVER_PORT
	# is set and we're in dual mode, the paper instance bumps it by one
	# so the two controllers don't collide on the same TCP port. Live
	# gets the configured port as-is. Single-mode passes through unchanged.
	if [ -n "${CONTROLLER_COMMAND_SERVER_PORT:-}" ] && [ "${DUAL_MODE:-}" = "yes" ]; then
		if [ "$TRADING_MODE" = "paper" ]; then
			_csp_base="${CONTROLLER_COMMAND_SERVER_PORT}"
			CONTROLLER_COMMAND_SERVER_PORT=$((_csp_base + 1))
			export CONTROLLER_COMMAND_SERVER_PORT
			echo ".>		command-server-port: ${CONTROLLER_COMMAND_SERVER_PORT} (dual-mode paper offset from ${_csp_base})"
		else
			echo ".>		command-server-port: ${CONTROLLER_COMMAND_SERVER_PORT} (dual-mode live)"
		fi
	elif [ -n "${CONTROLLER_COMMAND_SERVER_PORT:-}" ]; then
		echo ".>		command-server-port: ${CONTROLLER_COMMAND_SERVER_PORT}"
	fi

	# Dual-mode health server port offset (v0.4.9). Same pattern as the
	# command server: paper bumps by one so both controllers can bind on
	# the same container with a single env var. Dockerfile sets the
	# default to 8080; paper gets 8081 in dual-mode. scripts/healthcheck.sh
	# knows about the offset.
	if [ -n "${CONTROLLER_HEALTH_SERVER_PORT:-}" ] && [ "${DUAL_MODE:-}" = "yes" ]; then
		if [ "$TRADING_MODE" = "paper" ]; then
			_hsp_base="${CONTROLLER_HEALTH_SERVER_PORT}"
			CONTROLLER_HEALTH_SERVER_PORT=$((_hsp_base + 1))
			export CONTROLLER_HEALTH_SERVER_PORT
			echo ".>		health-server-port: ${CONTROLLER_HEALTH_SERVER_PORT} (dual-mode paper offset from ${_hsp_base})"
		else
			echo ".>		health-server-port: ${CONTROLLER_HEALTH_SERVER_PORT} (dual-mode live)"
		fi
	elif [ -n "${CONTROLLER_HEALTH_SERVER_PORT:-}" ]; then
		echo ".>		health-server-port: ${CONTROLLER_HEALTH_SERVER_PORT}"
	fi

	echo ".>		agent-socket: ${GATEWAY_INPUT_AGENT_SOCKET}"
	echo ".>		ready-file:   ${CONTROLLER_READY_FILE}"
	echo ".>		jts-config:   ${TWS_SETTINGS_PATH:-$TWS_PATH}"

	# Resolve credentials from secrets-style _FILE env vars if needed.
	# The controller reads TWS_USERID, TWS_PASSWORD, TRADING_MODE, and
	# TWOFACTOR_CODE directly from its environment.
	file_env 'TWS_PASSWORD'
	file_env 'TWOFACTOR_CODE'

	python3 "${SCRIPT_PATH}/gateway_controller.py" &
	_p="$!"
	pid+=("$_p")
	export pid
	echo "$_p" >"/tmp/pid_${TRADING_MODE}"

	unset_env 'TWS_PASSWORD'
	unset_env 'TWOFACTOR_CODE'
}

wait_for_controller_ready() {
	# Block until the controller signals readiness via its per-instance
	# ready file. This gates socat startup so API clients don't connect
	# before the Gateway login + main window is up.
	#
	# ALWAYS returns 0. run.sh is under `set -Eeo pipefail`, so a non-
	# zero return would abort the script. On timeout we only warn and
	# continue — the caller (start_process) still starts socat, and in
	# dual-mode the outer dispatch still proceeds to start the paper
	# instance. Letting a stuck live Gateway kill the whole container
	# before paper even gets a chance would be a regression against the
	# legacy IBC dual-mode behavior.
	local ready_file="${CONTROLLER_READY_FILE:-/tmp/gateway_ready}"
	echo ".> Waiting for Gateway controller to signal readiness (${ready_file})."
	local timeout=300
	local elapsed=0
	while [ ! -f "$ready_file" ] && [ $elapsed -lt $timeout ]; do
		sleep 1
		elapsed=$((elapsed + 1))
		if [ $((elapsed % 10)) -eq 0 ]; then
			echo ".>		(${elapsed}s elapsed)"
		fi
	done
	if [ ! -f "$ready_file" ]; then
		echo ".> WARNING: controller readiness timeout after ${timeout}s in ${TRADING_MODE} mode; starting socat anyway"
		return 0
	fi
	echo ".> Controller ready (${TRADING_MODE}) after ${elapsed}s."
	return 0
}

start_process() {
	# set API and socat ports
	set_ports
	# apply settings
	apply_settings

	if [ "$USE_IBG_CONTROLLER" = "yes" ]; then
		# Controller path: launch the controller first, wait for it to
		# signal readiness, THEN start port forwarding. This fixes the
		# long-standing issue where socat starts before Gateway is logged
		# in and accepting API connections.
		start_controller
		wait_for_controller_ready
		port_forwarding
	else
		# IBC path (default): legacy behavior — port forwarding starts
		# immediately, racing the IBC login flow.
		port_forwarding
		start_IBC
	fi
}

###############################################################################
#####		Common Start
###############################################################################

# run start scripts
if [ -n "$START_SCRIPTS" ]; then
	run_scripts "$HOME/$START_SCRIPTS"
fi

# start Xvfb
start_xvfb

# Window manager (only when using the ibg-controller path). Xvfb has no
# concept of focused window without a WM and Gateway's input routing
# depends on focus. Must come AFTER start_xvfb so DISPLAY is set, and
# BEFORE the controller launches Gateway.
if [ "$USE_IBG_CONTROLLER" = "yes" ]; then
	wait_x_socket
	start_window_manager
fi

# setup SSH Tunnel
setup_ssh

# Java heap size
set_java_heap

# start VNC server
start_vnc

# run scripts once X environment is up
if [ -n "$X_SCRIPTS" ]; then
	wait_x_socket
	run_scripts "$HOME/$X_SCRIPTS"
fi

###############################################################################
#####		Paper, Live or both start process
###############################################################################

if [ "$TRADING_MODE" == "both" ] || [ "$DUAL_MODE" == "yes" ]; then
	# start live and paper
	DUAL_MODE=yes
	export DUAL_MODE
	# start live first
	TRADING_MODE=live
	# add _live subfix
	_IBC_INI="${IBC_INI}"
	export _IBC_INI
	IBC_INI="${_IBC_INI}_${TRADING_MODE}"
	if [ -n "$TWS_SETTINGS_PATH" ]; then
		_TWS_SETTINGS_PATH="${TWS_SETTINGS_PATH}"
		export _TWS_SETTINGS_PATH
		TWS_SETTINGS_PATH="${_TWS_SETTINGS_PATH}_${TRADING_MODE}"
	else
		# no TWS settings
		_TWS_SETTINGS_PATH="${TWS_PATH}"
		export _TWS_SETTINGS_PATH
		TWS_SETTINGS_PATH="${_TWS_SETTINGS_PATH}_${TRADING_MODE}"
	fi
fi

start_process

if [ "$DUAL_MODE" == "yes" ]; then
	# running dual mode, start paper
	TRADING_MODE=paper
	TWS_USERID="${TWS_USERID_PAPER}"
	export TWS_USERID

	# handle password for dual mode
	if [ -n "${TWS_PASSWORD_PAPER_FILE}" ]; then
		TWS_PASSWORD_FILE="${TWS_PASSWORD_PAPER_FILE}"
		export TWS_PASSWORD_FILE
	else
		TWS_PASSWORD="${TWS_PASSWORD_PAPER}"
		export TWS_PASSWORD
	fi
	# disable duplicate ssh for vnc/rdp
	SSH_VNC_PORT=
	export SSH_VNC_PORT
	# in dual mode, ssh remote always == api port
	SSH_REMOTE_PORT=
	export SSH_REMOTE_PORT
	#
	IBC_INI="${_IBC_INI}_${TRADING_MODE}"
	TWS_SETTINGS_PATH="${_TWS_SETTINGS_PATH}_${TRADING_MODE}"

	# Stagger before starting the second instance. Inherited default of
	# 15s is what the legacy IBC dual-mode block used; the controller
	# path is faster and can usually get away with less. Override via
	# DUAL_MODE_STAGGER_SECONDS (integer seconds, 0 = no stagger).
	_stagger="${DUAL_MODE_STAGGER_SECONDS:-15}"
	echo ".> Dual-mode stagger: sleeping ${_stagger}s before starting paper instance"
	sleep "$_stagger"
	start_process
fi

# run scripts once IBC is running
if [ -n "$IBC_SCRIPTS" ]; then
	run_scripts "$HOME/$IBC_SCRIPTS"
fi

# Controller-path analog: CONTROLLER_SCRIPTS runs user scripts after
# the controller has signalled readiness (and in dual mode after BOTH
# instances have signalled). Users who previously used IBC_SCRIPTS for
# post-login hooks can use this with the same directory-of-shell-scripts
# semantics.
if [ "$USE_IBG_CONTROLLER" = "yes" ] && [ -n "${CONTROLLER_SCRIPTS:-}" ]; then
	run_scripts "$HOME/$CONTROLLER_SCRIPTS"
fi

trap stop_ibc SIGINT SIGTERM
wait "${pid[@]}"
exit $?
