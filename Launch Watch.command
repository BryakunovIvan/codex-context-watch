#!/bin/zsh
# Double-click on macOS to open the live monitor in Terminal.
tool_directory=${0:A:h}
exec python3 "$tool_directory/codex-context" watch "$@"
