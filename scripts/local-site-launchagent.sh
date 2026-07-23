#!/bin/zsh
set -euo pipefail

LABEL="com.matija.tundi-trip.local-site"
LEGACY_LABEL="com.matija.tundi-trip.preview"
PORT="8799"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
RUNNER="$SCRIPT_DIR/serve-local-site.sh"
BUILD_SCRIPT="$REPO_ROOT/deploy/build.sh"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OUT_LOG="$HOME/Library/Logs/tundi-trip-local-site.out.log"
ERR_LOG="$HOME/Library/Logs/tundi-trip-local-site.err.log"
HEALTH_URL="http://127.0.0.1:$PORT/budapest-london/tripadvisor/index.html"
TRIP_IDEAS_URL="http://127.0.0.1:$PORT/trip-ideas.html"

site_is_healthy() {
  /usr/bin/curl --connect-timeout 1 --max-time 2 -fsS -o /dev/null "$HEALTH_URL" \
    && /usr/bin/curl --connect-timeout 1 --max-time 2 -fsS -o /dev/null "$TRIP_IDEAS_URL"
}

xml_escape() {
  print -rn -- "$1" | /usr/bin/sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\&apos;/g"
}

launchagent_pid() {
  /bin/launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
    | /usr/bin/awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }'
}

listener_pid() {
  /usr/sbin/lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | /usr/bin/head -n 1
}

agent_owns_port() {
  local job_pid="$(launchagent_pid || true)"
  local port_pid="$(listener_pid || true)"
  [[ -n "$job_pid" && "$job_pid" == "$port_pid" ]]
}

print_status() {
  if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    print "loaded: $DOMAIN/$LABEL"
  else
    print "not loaded: $DOMAIN/$LABEL"
    return 1
  fi
  if ! agent_owns_port; then
    print -u2 "unhealthy: LaunchAgent does not own port $PORT"
    print -u2 "job pid: $(launchagent_pid || true); listener pid: $(listener_pid || true)"
    return 1
  fi
  site_is_healthy
  print "healthy: $HEALTH_URL"
  print "healthy: $TRIP_IDEAS_URL"
  /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true
}

uninstall_agent() {
  if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootout "$DOMAIN/$LABEL"
  fi
  /bin/rm -f "$PLIST"
  print "uninstalled: $LABEL"
}

install_agent() {
  if [[ ! -x "$BUILD_SCRIPT" ]]; then
    print -u2 "missing executable build script: $BUILD_SCRIPT"
    exit 1
  fi
  "$BUILD_SCRIPT"

  /bin/mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  /usr/bin/touch "$OUT_LOG" "$ERR_LOG"

  local plist_tmp="$PLIST.tmp.$$"
  local runner_xml="$(xml_escape "$RUNNER")"
  local root_xml="$(xml_escape "$REPO_ROOT")"
  local out_xml="$(xml_escape "$OUT_LOG")"
  local err_xml="$(xml_escape "$ERR_LOG")"

  /bin/cat >"$plist_tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$runner_xml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$root_xml</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>CUTETRIP_PORT</key>
    <string>$PORT</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$out_xml</string>
  <key>StandardErrorPath</key>
  <string>$err_xml</string>
</dict>
</plist>
EOF
  /usr/bin/plutil -lint "$plist_tmp" >/dev/null

  if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootout "$DOMAIN/$LABEL"
  fi
  if /bin/launchctl print "$DOMAIN/$LEGACY_LABEL" >/dev/null 2>&1; then
    print "migrating legacy job: $LEGACY_LABEL"
    /bin/launchctl bootout "$DOMAIN/$LEGACY_LABEL"
  fi

  local listener_pid listener_command
  for listener_pid in $(/usr/sbin/lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
    listener_command="$(/bin/ps -p "$listener_pid" -o command=)"
    if [[ "$listener_command" == *"http.server $PORT"* && "$listener_command" == *"$REPO_ROOT"* ]]; then
      /bin/kill "$listener_pid"
      for _ in {1..30}; do
        /bin/kill -0 "$listener_pid" 2>/dev/null || break
        /bin/sleep 0.1
      done
    else
      print -u2 "port $PORT is occupied by an unrelated process: $listener_command"
      /bin/rm -f "$plist_tmp"
      exit 1
    fi
  done

  /bin/mv "$plist_tmp" "$PLIST"
  /bin/launchctl bootstrap "$DOMAIN" "$PLIST"

  local deadline=$((SECONDS+10))
  while (( SECONDS < deadline )); do
    if agent_owns_port && site_is_healthy 2>/dev/null; then
      print "installed: $DOMAIN/$LABEL"
      print "healthy: $HEALTH_URL"
      print "healthy: $TRIP_IDEAS_URL"
      print "logs: $OUT_LOG"
      print "      $ERR_LOG"
      return 0
    fi
    /bin/sleep 0.2
  done

  print -u2 "LaunchAgent loaded but health check failed: $HEALTH_URL or $TRIP_IDEAS_URL"
  /usr/bin/tail -n 30 "$ERR_LOG" >&2 || true
  exit 1
}

case "${1:-install}" in
  install) install_agent ;;
  uninstall) uninstall_agent ;;
  status) print_status ;;
  *) print -u2 "usage: $0 [install|status|uninstall]"; exit 2 ;;
esac
