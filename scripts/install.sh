#!/bin/bash
# Install the MeetingMind launch agent for the current user.
#
# This copies com.meetingmind.agent.plist into ~/Library/LaunchAgents,
# substituting the /Users/USER/ placeholder with the real home directory,
# then loads the agent via launchctl.
#
# Usage: ./scripts/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PLIST_SRC="$PROJECT_ROOT/com.meetingmind.agent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.meetingmind.agent.plist"
LOG_DIR="$HOME/Library/Logs/meetingmind"
USERNAME="$(whoami)"

echo "==> Installing launch agent for $USERNAME"

# Substitute the /Users/USER/ placeholder with the real path.
sed "s|/Users/USER/|/Users/$USERNAME/|g" "$PLIST_SRC" > "$PLIST_DST"

# Ensure the log directory exists.
mkdir -p "$LOG_DIR"

# Load the agent (unload first if already loaded, to pick up changes).
if launchctl list com.meetingmind.agent &>/dev/null; then
    echo "==> Unloading existing agent"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

launchctl load "$PLIST_DST"

echo ""
echo "==> Launch agent installed"
echo "    Plist:  $PLIST_DST"
echo "    Logs:   $LOG_DIR/"
echo ""
echo "To uninstall:"
echo "    launchctl unload $PLIST_DST"
echo "    rm $PLIST_DST"
