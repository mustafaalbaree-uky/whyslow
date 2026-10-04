#!/bin/sh
# Bare `whyslow` in a terminal opens a Claude session that runs the whyslow skill.
# Flags, pipes and calls from inside Claude still run the program itself.
if [ $# -eq 0 ] && [ -t 0 ] && [ -t 1 ] && [ -z "$CLAUDECODE" ]; then
  exec claude whyslow
fi
exec /usr/bin/env python3 "$HOME/Code/whyslow/whyslow.py" "$@"
