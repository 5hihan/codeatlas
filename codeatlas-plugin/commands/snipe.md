---
description: Fix the next concrete issue (or the given issue id / failure) with the SNIPE loop
argument-hint: [issue id, failure text, or empty for next_snipe]
agent: codeatlas-sniper
---

Run the SNIPE loop. Target: $ARGUMENTS
(If empty, take `next_snipe`. If it looks like test or error output, feed it to `snipe_report` first.)
