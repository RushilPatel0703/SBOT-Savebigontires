"""Pause and resume for a long scrape: an append-only journal of finished work.

A North Country run is hours of sequential requests, and everything it found
used to live in memory until the very end. A dead session at term 1,445 of
1,452 threw away an 11-hour full run on 24 October, and a power cut during
the brand sweep on 25 October threw away that step.

Each finished unit of work (a search term, a ProductInfo or cost batch, a
brand) is appended to a JSON-lines journal and fsynced before the scraper
moves on. Restarted with --resume, the scraper replays the journal and carries
on from the first unit it has no record of. A pause, a crash and a power cut
all leave the same journal behind, so they all resume the same way.

Append-only is the point. Nothing already written is rewritten, so a write cut
off mid-line costs only that line, and a torn last line is dropped on resume.

Pausing is a flag file, PAUSE, in the supplier's run-state directory. A scraper
checks it between units, finishes the unit in hand, consumes the flag and exits
with PAUSE_EXIT, so the runner can tell "stopped on request" from "failed". It
is a file rather than a signal because the dashboard launches pipelines as
detached process trees, where reaching the right child with a signal differs
between Windows and macOS and a file does not.

Everything lives in <supplier>/Data/run_state/, which is gitignored. The
journals are .jsonl rather than .csv so the latest-raw-file globs in clean.py
and brand_sweep.py can never mistake one for a finished scrape.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

PAUSE_EXIT = 75   # sysexits EX_TEMPFAIL: stopped on request, resume later


def state_dir(supplier_dir) -> Path:
    return Path(supplier_dir) / "Data" / "run_state"


def pause_file(supplier_dir) -> Path:
    return state_dir(supplier_dir) / "PAUSE"


def pause_requested(supplier_dir) -> bool:
    return pause_file(supplier_dir).exists()


def request_pause(supplier_dir) -> None:
    path = pause_file(supplier_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now().isoformat(), encoding="utf-8")


def clear_pause(supplier_dir) -> None:
    pause_file(supplier_dir).unlink(missing_ok=True)


class Journal:
    """One scrape's journal. Use Journal.begin(), not the constructor."""

    def __init__(self, path: Path, meta: dict, entries: list, resumed: bool):
        self.path = path
        self.meta = meta
        self.resumed = resumed
        self._entries = entries

    @classmethod
    def begin(cls, path, meta: dict, resume: bool = False, match: tuple = ()) -> "Journal":
        """Open the journal for a run.

        resume=True replays an existing journal, provided its meta agrees with
        `meta` on every key in `match` (a brand sweep's base file, say). The
        stored meta then wins, so a resumed run searches the terms it planned
        at the start even if the zero-terms cache has moved since. Without
        resume, or with nothing usable to resume, any old journal is discarded
        and a fresh one is started.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if resume and path.exists():
            entries = _read(path)
            start = entries[0] if entries and entries[0].get("kind") == "start" else None
            if start is None:
                print(f"  [resume] {path.name} has no usable start record — starting fresh")
            elif any(start["meta"].get(k) != meta.get(k) for k in match):
                print(f"  [resume] {path.name} belongs to a different run "
                      f"({', '.join(match)} changed) — starting fresh")
            else:
                age_h = (datetime.now() - datetime.fromisoformat(start["at"])).total_seconds() / 3600
                print(f"  [resume] replaying {len(entries) - 1} journal entries from "
                      f"{start['at'][:16].replace('T', ' ')} ({age_h:.1f} h ago)")
                if age_h > 24:
                    print(f"  [resume] WARNING: this progress is {age_h:.0f} h old. Stock and "
                          f"prices from then will be mixed with today's.")
                return cls(path, start["meta"], entries[1:], resumed=True)
        elif resume:
            print(f"  [resume] no progress to resume at {path.name} — starting fresh")
        if path.exists():
            if not resume:
                print(f"  [journal] discarding earlier progress in {path.name} (no --resume)")
            path.unlink()
        journal = cls(path, meta, [], resumed=False)
        journal._write({"kind": "start", "at": datetime.now().isoformat(), "meta": meta})
        return journal

    def entries(self, kind: str | None = None) -> list:
        """Replayed and new entries in journal order, all of them or one kind."""
        return [e for e in self._entries if kind is None or e.get("kind") == kind]

    def add(self, kind: str, **data) -> None:
        entry = {"kind": kind, **data}
        self._entries.append(entry)
        self._write(entry)

    def phase(self, name: str, label: str, total: int, unit: str) -> None:
        """Record that a phase has started, once, so progress can be shown as
        "<label>: <done> of <total> <unit>". Units of the phase are the
        entries whose kind is `name`."""
        if not any(e.get("name") == name for e in self.entries("phase")):
            self.add("phase", name=name, label=label, total=total, unit=unit)

    def finish(self) -> None:
        """The run's output is written; the journal has nothing left to say."""
        self.path.unlink(missing_ok=True)

    def _write(self, entry: dict) -> None:
        # Opened per entry, not held: one write per search term or batch is
        # cheap, and no open handle is left to stop Windows deleting the file.
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _read(path: Path) -> list:
    """Every complete entry. A torn last line (a write cut off by a crash) is
    dropped and truncated away, so the next append starts on a clean line."""
    raw = path.read_bytes()
    entries, good_end, pos = [], 0, 0
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        end = len(raw) if nl == -1 else nl + 1
        line = raw[pos:end].strip()
        if line:
            try:
                entries.append(json.loads(line))
            except ValueError:
                if end < len(raw):
                    # Damage in the middle is not a crash artifact; replaying
                    # around it would silently drop work.
                    print(f"  [resume] {path.name} is damaged mid-file — starting fresh")
                    return []
                break
        good_end = end
        pos = end
    if good_end < len(raw):
        with open(path, "r+b") as fh:
            fh.truncate(good_end)
    elif raw and not raw.endswith(b"\n"):
        # A whole entry cut off just before its newline: keep it, end the line.
        with open(path, "ab") as fh:
            fh.write(b"\n")
    return entries


# ── Running a scrape against a journal ───────────────────────────────────────

class Halt:
    """Stops queued work on a pause request or a dead session.

    A scraper submits every term to its executor up front, and leaving a
    `with ThreadPoolExecutor` block waits for all of them, even after one has
    raised. Each unit therefore asks halt() first and returns at once when it
    is set, so neither a pause nor a session death runs hundreds of further
    requests on its way out.
    """

    def __init__(self, supplier_dir):
        self.supplier_dir = supplier_dir
        self.paused = False
        self._event = threading.Event()

    def __call__(self) -> bool:
        if not self._event.is_set() and pause_requested(self.supplier_dir):
            self.paused = True
            self._event.set()
        return self._event.is_set()

    def stop(self) -> None:
        self._event.set()

    def exit_if_paused(self, done: str) -> None:
        if self.paused:
            clear_pause(self.supplier_dir)
            print(f"\nPAUSED after {done}. Progress is saved; carry on with --resume "
                  f"(the dashboard's Resume button).")
            sys.exit(PAUSE_EXIT)


def done_terms(journal: Journal) -> dict:
    """{term: entry} for every search term the journal has a trustworthy
    result for, in the order they were searched.

    A dead session returns zero results rather than an error, so the zero
    streak that led up to a SessionDeathError may be the session talking, not
    empty stock (on 24 October that streak hid ST255/85). The handler records
    those terms as retry, and they are searched again on resume.
    """
    done = {}
    for e in journal.entries():
        if e["kind"] == "term":
            done.pop(e["term"], None)
            done[e["term"]] = e
        elif e["kind"] == "session_death":
            for term in e.get("retry", []):
                done.pop(term, None)
    return done


def trailing_zero_terms(term_counts: dict) -> list:
    """The run of zero-result terms at the end of term_counts (insertion order)."""
    trailing = []
    for term, count in reversed(list(term_counts.items())):
        if count:
            break
        trailing.append(term)
    return trailing


def fetch_batches(journal: Journal, kind: str, label: str, guids: list, batch_size: int,
                  fetch, apply, halt: Halt, workers: int = 1) -> None:
    """Run fetch(batch) for every batch of guids the journal has no record of.

    Batches already in the journal are replayed through apply() first, without
    a request. Each new success is journaled and then applied. A batch whose
    fetch raises is reported and skipped, as before, but not journaled, so a
    resumed run asks for it again. A SessionDeathError (a BaseException) stops
    the queued batches and propagates; the caller decides what it voids.
    """
    done = set()
    for e in _live(journal, kind):
        apply(e["result"])
        done.update(e["guids"])
    todo = [g for g in guids if g not in done]
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    journal.phase(kind, label, -(-len(guids) // batch_size), "batches")
    if done:
        print(f"  [resume] {len(done)} of {len(guids)} already fetched, "
              f"{len(batches)} batch(es) to go")

    def run(batch):
        if halt():
            return batch, None
        try:
            return batch, fetch(batch)
        except Exception as e:                          # noqa: BLE001
            print(f"  WARNING: {label} batch failed: {e}")
            return batch, None
        except BaseException:
            halt.stop()
            raise

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch, result in pool.map(run, batches):
            if result is None:
                continue
            result = json.loads(json.dumps(result))     # the shape a replay will see
            journal.add(kind, guids=batch, result=result)
            apply(result)


def _live(journal: Journal, kind: str) -> list:
    """Entries of `kind` since a dead session last voided them, so a phase that
    came back empty end to end is fetched again instead of replayed."""
    live = []
    for e in journal.entries():
        if e["kind"] == kind:
            live.append(e)
        elif e["kind"] == "session_death" and e.get("discard") == kind:
            live = []
    return live


def summary(path) -> str | None:
    """'Images + specs: 120 of 335 batches' for the latest phase of a journal,
    or None when there is no journal. Counts lines by their opening bytes
    rather than parsing them, so a multi-megabyte journal stays cheap."""
    path = Path(path)
    if not path.exists():
        return None
    counts, phases = {}, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith('{"kind": "'):
                continue
            kind = line[10:line.find('"', 10)]
            if kind == "phase":
                try:
                    phases.append(json.loads(line))
                except ValueError:
                    pass
            else:
                counts[kind] = counts.get(kind, 0) + 1
    if not phases:
        return None
    p = phases[-1]
    return f"{p['label']}: {min(counts.get(p['name'], 0), p['total'])} of {p['total']} {p['unit']}"


# ── Pipeline-level progress (which step a run had reached) ───────────────────

def progress_file(supplier_dir) -> Path:
    return state_dir(supplier_dir) / "pipeline_progress.json"


def load_progress(supplier_dir) -> dict | None:
    try:
        return json.loads(progress_file(supplier_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_progress(supplier_dir, progress: dict) -> None:
    path = progress_file(supplier_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_progress(supplier_dir) -> None:
    progress_file(supplier_dir).unlink(missing_ok=True)
