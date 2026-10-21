"""Replay a recording onto a local broker: telemetry, RGB and IR previews (optionally writer status), original topics.

Start a local broker first:  mosquitto -p 1884
Then:  python tools/mqtt/replay.py data/captures/NAME [--port 1884] [--speed 1] [--start HH:MM:SS] [--end HH:MM:SS]
                                  [--loop] [--no-ir] [--no-rgb] [--writer-status]
Each message is re-stamped (*_utc_ms, *_utc_ns, timestamp_ns) by one offset, replay time minus recorded receive time, so
consumers see live data and the spacing inside a message is untouched. Between messages the spacing is divided by the
speed, so offsets drift between messages when speed != 1: keep 1x where alignment across topics matters.
IR frames are read from the local recording and published from memory; nothing is written. HH:MM:SS is local time;
an --end before --start wraps past midnight only for a capture that crosses it.
A pass that publishes nothing is an error (so --loop over an empty window exits).
"""
import argparse
import base64
import heapq
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Callable, NamedTuple, Optional

if __package__:
    from .common import TELEMETRY, make_client
else:
    from common import TELEMETRY, make_client

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
RECV_MS = re.compile(rb'"recv_ms": (\d+)')
DAY_MS = 86_400_000
NO_IMAGE, MISSING_IMAGE = "no image recorded", "image file missing"


class Event(NamedTuple):
    rec_ms: int
    topic: str
    kind: str  # telemetry | rgb | ir | writer
    load: Callable[[], Optional[dict]]  # payload as recorded; None when its image file is missing


def stamp_unit(key):
    """Milliseconds per unit for wall-clock publisher fields; 0 for everything else (ages, durations, monotonic)."""
    if key.endswith("_utc_ms"):
        return 1
    if key.endswith(("_utc_ns", "timestamp_ns")):
        return 1_000_000
    return 0


def restamp(obj, offset_ms):
    """Copy of a payload with every wall-clock field moved by offset_ms."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            unit = stamp_unit(key) if isinstance(key, str) else 0
            # Epoch-scale only: a 0 or other sentinel in a time field must stay as it is.
            if unit and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 1e11 * unit:
                out[key] = value + offset_ms * unit
            else:
                out[key] = restamp(value, offset_ms)
        return out
    if isinstance(obj, list):
        return [restamp(v, offset_ms) for v in obj]
    return obj


def clock_ms(text, ref_ms):
    """HH:MM[:SS] in local time on the day of ref_ms, as epoch ms."""
    try:
        h, m, s = ([int(p) for p in text.split(":")] + [0, 0])[:3]
        day = datetime.fromtimestamp(ref_ms / 1000).replace(hour=h, minute=m, second=s, microsecond=0)
    except ValueError:
        raise SystemExit(f"bad time {text!r}, expected HH:MM:SS")
    return int(day.timestamp() * 1000)


def resolve_host(host):
    """Loopback only: replaying recorded printer topics onto the event broker would impersonate the printer."""
    if host is None:
        return "127.0.0.1"
    if host in LOCAL_HOSTS:
        return host
    raise SystemExit(f"refusing {host}: replay only publishes to a broker on this machine")


def _rows(path, start_ms, end_ms):
    """(recv_ms, raw line) inside [start, end); the recorders write in receive order, so stop at the end."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        for line in f:
            m = RECV_MS.search(line)
            ms = int(m.group(1)) if m else json.loads(line)["recv_ms"]
            if end_ms is not None and ms >= end_ms:
                return
            if start_ms is None or ms >= start_ms:
                yield ms, line


def _telemetry(rec, start_ms, end_ms):
    for ms, line in _rows(os.path.join(rec, "telemetry.jsonl"), start_ms, end_ms):
        yield Event(ms, TELEMETRY, "telemetry", lambda line=line: json.loads(line)["msg"])


def _previews(rec, name, kinds, start_ms, end_ms, skips):
    for ms, line in _rows(os.path.join(rec, name), start_ms, end_ms):
        meta = json.loads(line)
        topic, file = meta.pop("topic"), meta.pop("file", None)
        meta.pop("recv_ms")
        kind = "ir" if "/preview/ir/" in topic else "rgb"
        if kind not in kinds:
            continue
        if file:
            yield Event(ms, topic, kind, lambda f=file, m=meta: _with_image(rec, f, m))
        else:  # rows from before IR was recorded carry metadata only
            skips[NO_IMAGE] += 1


def _with_image(rec, file, meta):
    path = os.path.join(rec, file)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return {**meta, "jpeg_base64": base64.b64encode(f.read()).decode()}


def _writer(rec, start_ms, end_ms):
    for ms, line in _rows(os.path.join(rec, "writer.jsonl"), start_ms, end_ms):
        row = json.loads(line)
        yield Event(ms, row["topic"], "writer", lambda msg=row["msg"]: msg)


def _last_ms(path):
    """recv_ms of the last row of a file, read from its tail; None when absent or unreadable."""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        size, block = f.seek(0, 2), 1 << 16
        while True:
            f.seek(max(0, size - block))
            lines = f.read().strip().splitlines()
            if len(lines) > 1 or size <= block:
                break
            block *= 4
    if not lines:
        return None
    try:
        m = RECV_MS.search(lines[-1])
        return int(m.group(1)) if m else json.loads(lines[-1])["recv_ms"]
    except (ValueError, KeyError):  # a row still being written
        return None


def _extent(rec, pick):
    names = ("telemetry.jsonl", "previews.jsonl", "ir.jsonl", "writer.jsonl")
    times = [ms for ms in (pick(os.path.join(rec, n)) for n in names) if ms is not None]
    if not times:
        raise SystemExit(f"{rec} has no telemetry.jsonl, previews.jsonl, ir.jsonl or writer.jsonl")
    return times


def capture_start_ms(rec):
    """Receive time of the first recorded message of any kind."""
    return min(_extent(rec, lambda path: next(_rows(path, None, None), (None,))[0]))


def capture_end_ms(rec):
    """Receive time of the last recorded message of any kind."""
    return max(_extent(rec, _last_ms))


def load_events(rec, start_ms=None, end_ms=None, ir=True, rgb=True, writer=False, skips=None):
    """Lazy merge of all sources in receive-time order (ties keep telemetry, previews, ir, writer order)."""
    kinds = {k for k, on in (("ir", ir), ("rgb", rgb)) if on}
    skips = Counter() if skips is None else skips
    streams = [_telemetry(rec, start_ms, end_ms),
               _previews(rec, "previews.jsonl", kinds, start_ms, end_ms, skips),
               _previews(rec, "ir.jsonl", kinds, start_ms, end_ms, skips)]
    if writer:
        streams.append(_writer(rec, start_ms, end_ms))
    return heapq.merge(*streams, key=lambda e: e.rec_ms)


def play(events, publish, speed=1.0, counts=None, skips=None, clock=time.time, sleep=time.sleep):
    """One pass: publish(topic, json text) at the recorded spacing / speed.

    counts (published per topic) and skips (per reason) accumulate in the Counters passed in, so an interrupted
    pass is still counted; both are returned."""
    counts = Counter() if counts is None else counts
    skips = Counter() if skips is None else skips
    t0_rec = t0_wall = None
    for ev in events:
        msg = ev.load()
        if t0_rec is None:
            t0_rec, t0_wall = ev.rec_ms, clock() * 1000
        due = t0_wall + (ev.rec_ms - t0_rec) / speed
        wait = due - clock() * 1000
        if wait > 0:
            sleep(wait / 1000)
        if msg is None:
            skips[MISSING_IMAGE] += 1
            continue
        publish(ev.topic, json.dumps(restamp(msg, round(due) - ev.rec_ms), separators=(",", ":")))
        counts[ev.topic] += 1
    return counts, skips


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("recording")
    p.add_argument("--host", help="a loopback address; anything else is refused")
    p.add_argument("--port", type=int, default=1884)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--start", help="HH:MM:SS local, default: from the beginning")
    p.add_argument("--end", help="HH:MM:SS local, exclusive, default: to the end")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--no-ir", action="store_true")
    p.add_argument("--no-rgb", action="store_true")
    p.add_argument("--writer-status", action="store_true", help="also replay recorded writer status and results")
    args = p.parse_args(argv)
    if not args.speed > 0:
        raise SystemExit("--speed must be positive")
    host = resolve_host(args.host)

    first = capture_start_ms(args.recording)
    start_ms = clock_ms(args.start, first) if args.start else None
    end_ms = clock_ms(args.end, first) if args.end else None
    if start_ms is not None and end_ms is not None and end_ms <= start_ms:
        day = lambda ms: datetime.fromtimestamp(ms / 1000).date()
        if day(first) == day(capture_end_ms(args.recording)):
            raise SystemExit(f"--end {args.end} is not after --start {args.start} "
                             "and the capture does not cross midnight")
        end_ms += DAY_MS

    client = make_client("replay")
    try:
        client.connect(host, args.port)
    except OSError as e:
        raise SystemExit(f"cannot reach the broker at {host}:{args.port}: {e}")
    client.loop_start()
    state = {"last": None, "failed": 0}

    def publish(topic, payload):
        info = client.publish(topic, payload, qos=0)
        state["last"] = info
        state["failed"] += info.rc != 0

    total, skips, error = Counter(), Counter(), None
    print(f"replaying {args.recording} at {args.speed}x to {host}:{args.port}", flush=True)
    if args.speed != 1:
        print(f"speed {args.speed}x: time alignment across topics is exact only at 1x", file=sys.stderr)
    try:
        while True:
            before = sum(total.values())
            play(load_events(args.recording, start_ms, end_ms, ir=not args.no_ir, rgb=not args.no_rgb,
                             writer=args.writer_status, skips=skips), publish, args.speed, total, skips)
            if sum(total.values()) == before:
                error = "nothing to replay: check --start/--end and which files the recording has"
                break
            if not args.loop:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if state["last"]:
                state["last"].wait_for_publish(5)
        except (RuntimeError, ValueError):
            pass
        client.loop_stop()
        client.disconnect()
    skipped = ", ".join(f"{n} {why}" for why, n in sorted(skips.items())) or "none"
    print(f"published {sum(total.values())} messages {dict(sorted(total.items()))}"
          f"; skipped: {skipped}; {state['failed']} failed", flush=True)
    if error:
        raise SystemExit(error)
    return total


if __name__ == "__main__":
    main()
