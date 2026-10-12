#!/bin/sh
# Install (or remove) the Just Fill it! helper as a macOS login item, so the extension works without
# ever opening a terminal: launchd starts `uv run just-fill-it` at login and restarts it if it exits.
#
#   sh scripts/install_helper.sh                        # job-hunter next to this repo
#   sh scripts/install_helper.sh --job-hunter /path/to/job-hunter
#   sh scripts/install_helper.sh --uninstall
#
# Logs: logs/helper.log. The extension token is in state/token.
set -eu

LABEL="com.justfillit.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DOMAIN="gui/$(id -u)"
JOB_HUNTER="$(cd "$ROOT/.." && pwd)/job-hunter"

case "${1:-}" in
  --uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL."
    exit 0 ;;
  --job-hunter)
    JOB_HUNTER=$(cd "${2:?--job-hunter needs a path}" && pwd) ;;
esac

[ -f "$JOB_HUNTER/pyproject.toml" ] || { echo "job-hunter not found at $JOB_HUNTER (use --job-hunter PATH)." >&2; exit 1; }
UV=$(command -v uv || true)
CLAUDE=$(command -v claude || true)
if [ -z "$UV" ] || [ -z "$CLAUDE" ]; then
  echo "Need both uv and claude on PATH (found uv='$UV' claude='$CLAUDE')." >&2
  exit 1
fi
PATH_DIRS="$(dirname "$UV"):$(dirname "$CLAUDE"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
mkdir -p "$ROOT/logs" "$HOME/Library/LaunchAgents"

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$UV")</string>
    <string>run</string>
    <string>just-fill-it</string>
    <string>--job-hunter</string>
    <string>$(xml_escape "$JOB_HUNTER")</string>
    <string>--claude-bin</string>
    <string>$(xml_escape "$CLAUDE")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$ROOT")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml_escape "$PATH_DIRS")</string>
    <key>HOME</key><string>$(xml_escape "$HOME")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$(xml_escape "$ROOT/logs/helper.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$ROOT/logs/helper.log")</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL (starts at login). Waiting for the helper..."
i=0
until curl -fs "http://127.0.0.1:8767/api/health" >/dev/null 2>&1 || [ $i -ge 30 ]; do sleep 1; i=$((i + 1)); done
if curl -fs "http://127.0.0.1:8767/api/health" >/dev/null 2>&1; then
  echo "Helper is running. Token for the extension: $ROOT/state/token"
else
  echo "Helper didn't start yet; check $ROOT/logs/helper.log" >&2
fi
