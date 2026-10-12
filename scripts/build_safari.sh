#!/bin/sh
# Build the Safari version of the extension and install it to ~/Applications. The Xcode project
# (safari/) references extension/ directly; Safari picks up changes once the app is rebuilt and opened.
#
#   sh scripts/build_safari.sh          # signed with your Apple development team (see below)
#   sh scripts/build_safari.sh --adhoc  # no Apple ID: local signing; needs Safari > Develop >
#                                       # Allow Unsigned Extensions after each Safari restart
#
# The team is never committed (this repo is public): set DEVELOPMENT_TEAM, or the script reads the
# first team Xcode knows (Xcode > Settings > Accounts; a free Personal Team works, but its
# signatures expire after about a week, so rebuild when the extension stops loading).
# Needs Xcode; uses the selected one, or /Applications or ~/Downloads Xcode.app.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP="Just Fill it"
PROJECT_DIR="$ROOT/safari/$APP"

if ! xcodebuild -version >/dev/null 2>&1; then
  for candidate in /Applications/Xcode.app "$HOME/Downloads/Xcode.app" "$HOME/Applications/Xcode.app"; do
    if [ -d "$candidate/Contents/Developer" ]; then
      DEVELOPER_DIR="$candidate/Contents/Developer"
      export DEVELOPER_DIR
      break
    fi
  done
fi
xcodebuild -version >/dev/null 2>&1 || { echo "Xcode not found; install it from the App Store." >&2; exit 1; }

if [ "${1:-}" = "--adhoc" ]; then
  set -- CODE_SIGN_IDENTITY="-" CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM=""
else
  TEAM="${DEVELOPMENT_TEAM:-$(defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier 2>/dev/null \
    | sed -n 's/.*teamID = \([A-Z0-9]*\);.*/\1/p' | head -1)}"
  if [ -z "$TEAM" ]; then
    echo "No Apple development team found: sign in under Xcode > Settings > Accounts," >&2
    echo "set DEVELOPMENT_TEAM, or run with --adhoc." >&2
    exit 1
  fi
  set -- -allowProvisioningUpdates CODE_SIGN_STYLE=Automatic DEVELOPMENT_TEAM="$TEAM"
fi

cd "$PROJECT_DIR"
xcodebuild -project "$APP.xcodeproj" -scheme "$APP" -configuration Release \
  -derivedDataPath build -quiet "$@" build

osascript -e "tell application \"$APP\" to quit" 2>/dev/null || true
sleep 2
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/$APP.app"
ditto "build/Build/Products/Release/$APP.app" "$HOME/Applications/$APP.app"
open "$HOME/Applications/$APP.app"
echo "Installed ~/Applications/$APP.app. In Safari: Settings > Extensions > Just Fill it!"
