# Product

## Register

product

## Users

The team running experiments at LEAM's mini delta printer during the Siemens × LEAM hackathon: two or three engineers
standing at the bench with laptops, watching the thermal camera while they deliberately push flow, pressure
advance, nozzle temperature, speed and fans to provoke extrusion defects for a training dataset. The same screen is what
we demo to LEAM and Siemens judges, sometimes on a projector. Glances last seconds; sessions last hours.

## Product Purpose

Extrusion Monitor runs on the Siemens Industrial Edge device next to the printer. It shows the live IR camera
feed, every telemetry value the printer publishes, and lets the team send absolute corrections through LEAM's command
gateway. Success means nobody ever misreads the printer: stale or invalid data is never shown as current, a sent
command's real outcome is obvious, and the operator can provoke a defect and see its effect within seconds.

## Brand Personality

Precise, tactile, trustworthy. It should feel like a real instrument on the bench, a DJ mixer or a good bench
oscilloscope: dense, calm, every control where the hand expects it, with a little hardware character (silkscreened
labels, faders, LED indicators, a meter bridge) that earns its place by carrying state.

## Anti-references

- Generic SaaS admin templates: card grids, hero metrics, gradient accents, Tailwind-UI defaults.
- Sci-fi neon HUDs: glowing cyan on black, glassmorphism, fake futurism. Real instruments don't glow for decoration.
- Clunky industrial HMIs (WinCC/SCADA): grey bevels, tiny crammed panels, no hierarchy.
- Toy or consumer-cute UI: bubbly shapes, playful color, emoji.

## Design Principles

1. Truth over polish: never show a value as live unless it is; freshness, validity and outcome are always visible.
2. Hardware metaphors only where they carry state: a fader is a setpoint, a meter is a readback, an LED is a status.
3. The camera is the hero; everything else frames it and stays out of its way.
4. One glance, one answer: what is the printer doing, what did I just change, did it take.
5. Hands-on layout: the operator decides what is big, what is beside it, and how much room each part gets.

## Accessibility & Inclusion

WCAG AA contrast for text and readouts. State is never color-only (LEDs carry a text label). Every control works from
the keyboard, including faders and pane splitters. Respect reduced motion. The IR feed is shown raw and unprocessed and
is never stored.
