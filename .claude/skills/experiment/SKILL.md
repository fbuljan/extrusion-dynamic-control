---
name: experiment
description: |
  Run a quick, timeboxed experiment and record the verdict. Use when trying an approach whose
  outcome is unknown: a bead-width method, a camera or calibration trick, an MQTT or Edge
  packaging step, a model. Write hypothesis and timebox first, run it in a scratch folder or a
  branch, record result and verdict (adopt / drop / park) in docs/experiments.md, and record any
  decision it settles in docs/decisions.md.
---

# experiment: hypothesis, timebox, verdict

A failed experiment that is written down is progress; a forgotten one gets repeated by a teammate.
Default timebox 30 minutes, 60 at most. Choose it before starting and say it out loud.

## 1. Write the entry before running

Append to `docs/experiments.md`. Name it with a short slug, not a number.

    ## <slug> | YYYY-MM-DD HH:MM | timebox 30 min | <who>
    Hypothesis: if we <approach>, then <measurable result>, because <reason>.
    Pass if: <number or observable, e.g. width error <= 0.1 mm on recorded frames>.
    Data: <recording name / calibration / live topic>.
    Where: <scratch path, or branch exp/<slug>>
    Status: running

## 2. Run it, fast loop first

- Build a check that answers in minutes before anything slow: recorded frames (`data/captures/`)
  against known ground truth, then live (read-only), then in a container, then on the device.
- Experimental code stays out of `main` until adopted: a scratch folder outside the repo, or a branch.
- Independent experiments can run as parallel subagents (see `fan-out`).
- At the timebox, stop and write down what you have. Extend once, only if the answer is clearly
  within reach, and say so.

## 3. Record result and verdict

Update the same entry in place.

    Result: <numbers; what it ran on: recorded / synthetic / live / container / device;
            what it did NOT run on>
    Verdict: adopt | drop | park
    Because: <one line>
    Next: <adopt: where it lands. park: what would reopen it. drop: nothing>

## 4. Promote

- **adopt:** move the code into the repo proper, and add a row to `docs/decisions.md` if it settles
  a choice. Check the diff: no credentials, broker addresses, organiser files or IR data.
- A durable fact the run established (a real mm/px, a payload field's meaning, a gotcha) goes into
  the relevant `docs/` file, not only into the experiment entry.
- **drop:** keep the entry; its "Because" stops anyone retrying. **park:** say what would reopen it.

## Never

- Send printer commands (flow %, pressure advance, any setpoint) or publish on organiser topics
  from an experiment, unless the team lead said so for this run.
- Write IR images anywhere.
- Call something "works" on reasoning alone. Say what was run and what was not.
