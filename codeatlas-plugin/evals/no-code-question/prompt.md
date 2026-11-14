---
schema_version: "1.1"
name: no-code-question
description: A question that has nothing to do with the repository must not trigger CodeAtlas tools.
tags: [negative]
plugins: ["../.."]
runs: 2
expected_outcome: A direct answer with no CodeAtlas tool call.
max_turns: 3
---
In two sentences, what is the difference between a mutex and a semaphore?
