#!/bin/sh
set -eu
printf 'owner,action,due\n' > "$2"
awk -F ' *\\| *' '/^- Action: / { sub(/^- Action: /, "", $1); print $1 "," $2 "," $3 }' "$1" >> "$2"
printf 'meeting-actions: converter completed\n'
