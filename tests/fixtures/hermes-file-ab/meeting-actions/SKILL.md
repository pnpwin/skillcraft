---
name: meeting-actions
description: Extract action items from meeting notes into a CSV deliverable.
---

Read `inputs/notes.md` using the file tool. Run this skill's bundled converter
with the terminal to create `outputs/actions.csv`. Resolve the absolute script
path from the preloaded skill directory.

```sh
/bin/bash /absolute/skill/directory/scripts/actions.sh inputs/notes.md outputs/actions.csv
```

Check the resulting file and report its location.
