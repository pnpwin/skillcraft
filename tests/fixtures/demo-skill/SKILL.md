---
name: demo-skill
description: Convert a short purchase receipt into normalized JSON with merchant, total, and currency. Use for receipt normalization or purchase-total extraction; not for arithmetic questions or translations.
---

Normalize the receipt into a single JSON object. Include merchant, total (a number),
currency (ISO code), and schema_version (the integer 1). For a dollar sign without
another country marker, use USD. Return the JSON as the final answer without Markdown fences.
