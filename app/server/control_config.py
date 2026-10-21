"""Controller limits, costs and hold thresholds: config, not code. `load(overrides)` validates, fills defaults."""
import copy

# The only operations the controller may ever return; pressure advance and fans stay manual (D19).
LEVERS = ("flow_percent", "speed_override_percent", "nozzle_temperature_c")
LEAM_RANGES = {"flow_percent": (0, 200), "speed_override_percent": (10, 200), "nozzle_temperature_c": (190, 250)}
LEVER_KEYS = {"bounds", "max_step", "cost", "effect_delay_s", "tolerance"}
# two_groups: when the print shows several views (each with its own camera offset), settle and act only on estimates
# that hold at least two of them; none: any estimate.
GROUP_RULES = ("none", "two_groups")

DEFAULTS = {
    "mode": "model",  # "model": printer-model increments over all levers; "fallback": flow-only proportional
    "levers": {
        # effect_delay_s: deposit time after confirmation before a sample can show the change (pressure / heater lag)
        "flow_percent": {"bounds": (70, 130), "max_step": 10, "cost": 1.0, "effect_delay_s": 3, "tolerance": 0.5},
        "speed_override_percent": {"bounds": (60, 100), "max_step": 10, "cost": 3.0, "effect_delay_s": 3,
                                   "tolerance": 0.5},
        "nozzle_temperature_c": {"bounds": (190, 250), "max_step": 5, "cost": 10.0, "effect_delay_s": 30,
                                 "tolerance": 0.5},
    },
    "kappa": 0.5,                # ask the model for this fraction of the gap per command
    "deadband_mm": 0.05,
    "sigma_factor": 2.0,         # also hold while |error| <= sigma_factor * estimate sigma
    # A rolling estimate wanders: the error must stay outside the band on one side this long, over this many
    # estimates. Ticks without a usable estimate count as time but not as estimates, until the last estimate off
    # target is persist_gap_s old (longer than a layer change or a blind side): then the run starts over.
    "persist_s": 10.0,
    "persist_estimates": 5,
    "persist_gap_s": 30.0,
    "min_width_mm": 1.30,        # below this the walls of a part may not touch: an alarm, not a hold
    # Below min_width_mm a real narrowing outweighs the risk of chasing noise: Auto acts on estimates down to this
    # confidence, ignores the noise band, and may take steps up to floor_step_factor x each lever's max_step (2x
    # ratcheted the bead wide on 3x frame noise, D48).
    "floor_min_confidence": 0.45,
    "floor_step_factor": 1.0,
    "floor_hold_s": 20.0,        # the floor rules stay on this long after the last estimate below it (noise bounces),
    "floor_release_mm": 0.05,    # while the estimate stays under min_width_mm + this
    "group_rule": "two_groups",
    "residual_weight": 20.0,
    "step_penalty": 0.5,
    # Two levers in one move (D53): where the bead is capped by both the asked flow and the melt rate, one lever alone
    # does almost nothing. Sent as two commands, the slower lever first, then settled once.
    "pair_moves": True,
    # Moving into the outer headroom_fraction of a lever's range costs up to headroom_weight (at the bound), so Auto
    # spreads a change over levers instead of pinning one at its limit; moving out of it is free.
    "headroom_fraction": 0.2,
    "headroom_weight": 10.0,  # as much as the nozzle's cost: a pinned lever is worth a slower one
    "min_effect_fraction": 0.2,  # best candidate must promise this share of the asked change, else hold
    "min_confidence": 0.5,
    "max_estimate_age_ms": 5000,
    "settle_samples": 6,         # ok samples deposited after the effect delay before a change counts as settled
    "min_spacing_ms": 2000,
    "pin_s": 300,
    "foreign_hold_s": 30,
    "result_timeout_ms": 3000,
    "readback_timeout_ms": 5000,  # after setting_confirmed
    "explain_wait_ms": 3000,      # after a failure, only a readback at least this much newer shows what happened
    "uncertain_memory_s": 60,     # an unconfirmed value may still land this long and is then ours, not external
    "advice_memory_s": 30,        # in advice mode, a readback equal to advice this recent is the advice applied
    "max_failures": 2,
    "readback_max_age_ms": 5000,
    "min_layer": 6,
    "ineffective_ratio": 0.3,
    "ineffective_z": 2.0,         # ineffective only if observed + z * sigma < ratio * predicted (sim: no false trips)
    "ineffective_steps": 2,
    "streak_gap_s": 120,          # steps further apart than this are separate episodes for the ineffective check
    "widen_response_ratio": 0.5,  # a widening step must show this share of its predicted change before the next
    "fallback_gain_floor_mm": 0.3,  # fallback divides by the estimate; never by less than this
    "anchor_operating_point": False,  # evaluate the model where it reproduces the estimate (a clog moves the point)
    "anchor_tau_s": 30.0,
    "anchor_range": (0.8, 1.2),
}
FLOORS = {"min_spacing_ms": (2000, None), "max_failures": (1, 3), "foreign_hold_s": (10, None), "pin_s": (60, None),
          "min_confidence": (0.3, None), "settle_samples": (3, None),
          "widen_response_ratio": (0.3, 1.0), "persist_s": (0, None), "persist_estimates": (1, None),
          "persist_gap_s": (1, None),
          "min_width_mm": (0.8, 2.0), "floor_min_confidence": (0.3, None),
          "floor_step_factor": (1.0, 2.0), "floor_hold_s": (0, 60), "floor_release_mm": (0, 0.2),
          "headroom_fraction": (0.0, 0.5), "headroom_weight": (0.0, None)}


# What the operator may set at runtime on the Test bench, and the range the app accepts (int: whole numbers only).
OPERATOR = {"min_width_mm": (0.8, 2.0, float), "persist_s": (0.0, 60.0, float), "persist_estimates": (1, 20, int),
            "kappa": (0.1, 1.0, float), "pin_s": (60.0, 900.0, float), "min_layer": (1, 50, int),
            "floor_step_factor": (1.0, 2.0, float)}


def load(overrides=None):
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key == "levers":  # merged per lever; None removes one
            for op, spec in value.items():
                if spec is None:
                    cfg["levers"].pop(op, None)
                else:
                    cfg["levers"][op] = {**cfg["levers"].get(op, {}), **spec}
        elif key in DEFAULTS:
            cfg[key] = value
        else:
            raise ValueError(f"unknown controller setting {key!r}")
    if cfg["mode"] not in ("model", "fallback"):
        raise ValueError("mode must be 'model' or 'fallback'")
    if cfg["group_rule"] not in GROUP_RULES:
        raise ValueError(f"group_rule must be one of {GROUP_RULES}")
    if cfg["mode"] == "fallback" and "flow_percent" not in cfg["levers"]:
        raise ValueError("fallback mode moves flow only, so flow_percent must stay a lever")
    for key, (low, high) in FLOORS.items():
        if not (low <= cfg[key] and (high is None or cfg[key] <= high)):
            raise ValueError(f"{key} must be {'>= ' + str(low) if high is None else f'{low}-{high}'}")
    for op, spec in cfg["levers"].items():
        if op not in LEVERS:
            raise ValueError(f"{op} is not a controller lever (allowed: {', '.join(LEVERS)})")
        if set(spec) != LEVER_KEYS:
            raise ValueError(f"{op} settings must be exactly {sorted(LEVER_KEYS)}, got {sorted(spec)}")
        lo, hi = spec["bounds"]
        leam_lo, leam_hi = LEAM_RANGES[op]
        if not leam_lo <= lo < hi <= leam_hi:
            raise ValueError(f"{op} bounds {lo}-{hi} outside LEAM's {leam_lo}-{leam_hi}")
        if not spec["max_step"] > 0:
            raise ValueError(f"{op} max_step must be > 0")
        if not spec["effect_delay_s"] >= 0:
            raise ValueError(f"{op} effect_delay_s must be >= 0")
        if not 0 < spec["tolerance"] <= spec["max_step"] / 2:
            raise ValueError(f"{op} tolerance must be within (0, max_step / 2]")
    if not 0 < cfg["kappa"] <= 1:
        raise ValueError("kappa must be within (0, 1]")
    if cfg["floor_min_confidence"] > cfg["min_confidence"]:
        raise ValueError("floor_min_confidence must be <= min_confidence")
    return cfg


def limits(cfg):
    """Read-only, JSON-safe view of what the controller may do."""
    levers = cfg["levers"] if cfg["mode"] == "model" else {"flow_percent": cfg["levers"]["flow_percent"]}
    return {"mode": cfg["mode"], "min_spacing_ms": cfg["min_spacing_ms"], "pin_s": cfg["pin_s"],
            "min_layer": cfg["min_layer"], "min_width_mm": cfg["min_width_mm"],
            "floor_step_factor": cfg["floor_step_factor"], "band_default_mm": cfg["deadband_mm"],
            "sigma_factor": cfg["sigma_factor"], "kappa": cfg["kappa"], "persist_s": cfg["persist_s"],
            "persist_estimates": cfg["persist_estimates"], "settle_samples": cfg["settle_samples"],
            "foreign_hold_s": cfg["foreign_hold_s"],
            "levers": {op: {"min": s["bounds"][0], "max": s["bounds"][1], "max_step": s["max_step"]}
                       for op, s in levers.items()}}
