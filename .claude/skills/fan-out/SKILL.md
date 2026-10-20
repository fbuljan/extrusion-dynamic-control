---
name: fan-out
description: |
  Spawn parallel subagents for research or independent prototypes, then merge. Use when there
  are two or more independent questions or sources: organiser documents, MQTT recordings,
  calibration maps, Siemens Edge packaging docs, competing width-measurement approaches. Not
  for tightly coupled building or for debugging one non-obvious failure; that needs one mind.
---

# fan-out: parallel agents, one merge

## 1. Split

- One agent per independent question, at most about five at once. If the answers depend on
  each other, run them in sequence or do it yourself.
- Write the question list down before launching so a context reset does not lose it.

## 2. Pick the model, say why in one line

- Haiku: lookups, find a topic or field, extract a table.
- Sonnet: most research, summarising sources, well-specified prototypes.
- Opus: width-algorithm design, hard debugging, anything near printer actuation.
- Start cheaper and escalate if the result is thin.

## 3. Brief each agent with five fields

    GOAL: <what and why, two lines>
    READ FIRST: <paths in docs/ and data/>; do not re-derive them
    QUESTIONS: <numbered, answerable>
    DELIVERABLE: <one file, plus code in its own folder>, <= N lines, every claim with its
                 evidence (file and line, sample message, numbers, URL). Tag findings
                 CONFIRMED / ASSUMPTION / OPEN.
    RULES: read-only except the deliverable; no git; no publishing to MQTT; never write IR
           images; no secrets copied into the repo; if the sources are silent or contradict
           each other, say so, do not guess. Reply with a short summary only.

## 4. Run in the background, in one message

Launch all agents in a single message, keep working meanwhile, and never wait with an
open-ended poll.

## 5. Merge

- Read the deliverables yourself. Open the cited source for every claim a decision will rest
  on; agents are sometimes confidently wrong.
- Facts go to the relevant `docs/` file, choices to `docs/decisions.md`, trials to `docs/experiments.md`.
- One correct answer: proceed. A real trade-off that is the team's call: ask, with a starred
  recommendation.

## 6. Say what was not covered

End with one line: what the agents did not read or run.
