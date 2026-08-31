#!/usr/bin/env python3
"""Local review app for tire photos — click the bad ones, nothing else.

    cd pipelines/shared
    python tread_review.py

Opens http://127.0.0.1:8777 in your browser. Every photo starts as KEEP, so on
a set that is ~91% good the work is clicking the ~9% that are wrong. Save, and
the labels go straight into tread_cache.json — no CSV, no import step.

WHY A LOCAL PAGE AND NOT A HOSTED ONE
-------------------------------------
The photos live on the suppliers' own CDNs (dttirehub.ca, northcountrytire.ca,
wheelmartcanada.ca). A page served from this machine loads them directly; a
hosted page cannot, and embedding 1,400 images as data URIs is both enormous
and pointless when the review is a local job anyway.

WHAT IT SHOWS
-------------
Each card carries the brand and model the photo is attached to, because "is
the tread facing me" is easier to answer when you can see it is a Wildpeak
A/T4W and not a slick. Cards are ordered so the same product's photo is never
split across two batches.

NOTHING HERE SPENDS SERPAPI CREDIT. It reads CSVs and fetches images the
quality audit already fetched.
"""
import argparse
import collections
import http.server
import json
import os
import socketserver
import sys
import threading
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tread                                                    # noqa: E402

DEFAULT_PORT = 8777
BATCH = 60


# ---------------------------------------------------------------------------
# Catalogue index — url -> what product it belongs to
# ---------------------------------------------------------------------------

def build_index(include_labelled=False):
    """[{url, brand, model, supplier, rows}] for every tire photo that passes
    the resolution gate, newest-gap-first, grouped so one product's photo is
    never split across batches."""
    import enrichment as E
    import pandas as pd

    quality = E.load_quality_cache()
    labels = tread.load()

    seen = {}
    for src in E.SOURCES:
        if src["field"] != "image" or src["type"] != "tires" or not src.get("value_col"):
            continue
        f = E.latest(src["glob"])
        if not f:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        if src["value_col"] not in df.columns:
            continue
        for _, row in df.iterrows():
            url = str(row.get(src["value_col"], "") or "").strip()
            if not url:
                continue
            verdict = E.image_verdict(url, quality)
            # "unknown" means nobody has MEASURED this URL, not that it is bad
            # — which is exactly the state every photo bought by an enrichment
            # run is in until the next scan. Queueing only "ok" dropped those
            # silently, so the photos most in need of a first look were the
            # ones the reviewer never saw. A genuinely bad photo still carries
            # a real verdict (missing / low_res / placeholder) and stays out.
            if verdict not in ("ok", "unknown"):
                continue
            entry = seen.get(url)
            if entry is None:
                brand, model = E._resolve_brand_model(row, src)
                entry = seen[url] = {
                    "url": url,
                    "brand": brand.strip(),
                    "model": model.strip(),
                    "supplier": src["supplier"],
                    "rows": 0,
                }
            entry["rows"] += 1

    items = []
    for entry in seen.values():
        current = tread.verdict(entry["url"], labels)
        if current != "unlabelled" and not include_labelled:
            continue
        entry = dict(entry, current=current)
        items.append(entry)

    # Group by product so a reviewer judging "Wildpeak A/T4W" sees all of its
    # photos together, and sort the biggest blast radius first — a photo on
    # 66 listings is worth more attention than one on a single row.
    items.sort(key=lambda e: (e["brand"].lower(), e["model"].lower()))
    items.sort(key=lambda e: -e["rows"])
    return items


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tread review</title>
<style>
  :root{
    --ground:#12151a; --card:#1b2027; --edge:#2b323b; --ink:#e9ece8;
    --soft:#98a29b; --keep:#5fb894; --rej:#e0684a; --unsure:#d6ac4e;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
  header{position:sticky;top:0;z-index:10;background:rgba(18,21,26,.96);
         backdrop-filter:blur(8px);border-bottom:1px solid var(--edge);
         padding:.85rem 1.25rem;display:flex;gap:1.25rem;align-items:center;
         flex-wrap:wrap}
  h1{font-size:1rem;margin:0;font-weight:600;letter-spacing:-.01em}
  .stat{font-family:var(--mono);font-size:.78rem;color:var(--soft)}
  .stat b{color:var(--ink);font-weight:600}
  .bar{flex:1;min-width:120px;height:5px;background:var(--edge);border-radius:3px;
       overflow:hidden}
  .bar i{display:block;height:100%;background:var(--keep);width:0}
  button{font:inherit;font-weight:600;border:0;border-radius:5px;padding:.5rem 1rem;
         cursor:pointer;background:var(--keep);color:#06231a}
  button:disabled{opacity:.45;cursor:default}
  button.ghost{background:transparent;color:var(--soft);border:1px solid var(--edge)}
  button:focus-visible{outline:2px solid var(--keep);outline-offset:2px}
  .hint{padding:.9rem 1.25rem;color:var(--soft);font-size:.85rem;
        border-bottom:1px solid var(--edge)}
  .hint kbd{font-family:var(--mono);font-size:.75rem;background:var(--card);
            border:1px solid var(--edge);border-radius:3px;padding:.05em .4em}
  .grid{display:grid;gap:.85rem;padding:1.25rem;
        grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
  .card{background:var(--card);border:2px solid var(--edge);border-radius:7px;
        overflow:hidden;cursor:pointer;user-select:none;position:relative;
        transition:border-color .12s}
  .card:focus-visible{outline:2px solid var(--keep);outline-offset:2px}
  .card .imgwrap{aspect-ratio:1;display:grid;place-items:center;background:#0d1013}
  .card img{max-width:100%;max-height:100%;display:block}
  .meta{padding:.45rem .6rem .55rem;border-top:1px solid var(--edge)}
  .meta .nm{font-size:.76rem;line-height:1.3;white-space:nowrap;overflow:hidden;
            text-overflow:ellipsis}
  .meta .sub{font-family:var(--mono);font-size:.66rem;color:var(--soft);margin-top:.15rem}
  .flag{position:absolute;top:.5rem;left:.5rem;font-family:var(--mono);
        font-size:.65rem;font-weight:700;letter-spacing:.08em;padding:.2em .5em;
        border-radius:3px;text-transform:uppercase}
  .card[data-s="v"]{border-color:var(--edge)}
  .card[data-s="v"] .flag{display:none}
  .card[data-s="n"]{border-color:var(--rej)}
  .card[data-s="n"] .flag{background:var(--rej);color:#2a0b04}
  .card[data-s="n"] .imgwrap{opacity:.4}
  .card[data-s="u"]{border-color:var(--unsure)}
  .card[data-s="u"] .flag{background:var(--unsure);color:#2b2205}
  footer{position:sticky;bottom:0;background:rgba(18,21,26,.96);
         backdrop-filter:blur(8px);border-top:1px solid var(--edge);
         padding:.85rem 1.25rem;display:flex;gap:1rem;align-items:center;
         flex-wrap:wrap}
  .done{padding:4rem 1.25rem;text-align:center;color:var(--soft)}
</style></head><body>
<header>
  <h1>Tread review</h1>
  <div class="stat"><b id="nrej">0</b> rejected in this batch</div>
  <div class="bar"><i id="bar"></i></div>
  <div class="stat"><b id="prog">0</b> / <span id="total">0</span> labelled</div>
</header>
<div class="hint">
  Every photo is <b>keep</b> by default. <b>Click</b> the ones where you see the
  side of the tire instead of the tread. Click again for <kbd>?</kbd> (unsure),
  once more to go back to keep.
</div>
<div class="grid" id="grid"></div>
<footer>
  <button id="save">Save and load next</button>
  <button class="ghost" id="skip">Skip this batch</button>
  <span class="stat" id="msg"></span>
</footer>
<script>
const NEXT = {v:"n", n:"u", u:"v"};
let batch = [];

function esc(s){ return String(s||"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function counts(){
  const n = document.querySelectorAll('.card[data-s="n"]').length;
  document.getElementById("nrej").textContent = n;
}

function render(items, prog, total){
  batch = items;
  const grid = document.getElementById("grid");
  document.getElementById("prog").textContent = prog;
  document.getElementById("total").textContent = total;
  document.getElementById("bar").style.width =
    (total ? (100*prog/total) : 0).toFixed(1) + "%";

  if(!items.length){
    grid.innerHTML = '<div class="done">Every photo has been reviewed. '
      + 'Nothing left in the queue.</div>';
    document.getElementById("save").disabled = true;
    document.getElementById("skip").disabled = true;
    return;
  }
  grid.innerHTML = items.map((it,i) => `
    <div class="card" data-s="v" data-i="${i}" tabindex="0"
         title="${esc(it.url)}">
      <span class="flag">reject</span>
      <div class="imgwrap"><img loading="lazy" src="${esc(it.url)}" alt=""></div>
      <div class="meta">
        <div class="nm">${esc(it.brand)} ${esc(it.model)}</div>
        <div class="sub">${esc(it.supplier)} &middot; ${it.rows} row${it.rows==1?"":"s"}</div>
      </div>
    </div>`).join("");
  counts();
}

function cycle(card){
  card.dataset.s = NEXT[card.dataset.s] || "n";
  card.querySelector(".flag").textContent =
    card.dataset.s === "n" ? "reject" : "unsure";
  counts();
}

document.getElementById("grid").addEventListener("click", e => {
  const card = e.target.closest(".card");
  if(card) cycle(card);
});
document.getElementById("grid").addEventListener("keydown", e => {
  if(e.key !== "Enter" && e.key !== " ") return;
  const card = e.target.closest(".card");
  if(card){ e.preventDefault(); cycle(card); }
});

async function load(){
  const r = await fetch("/api/batch");
  const d = await r.json();
  render(d.items, d.labelled, d.total);
  window.scrollTo(0,0);
}

document.getElementById("save").addEventListener("click", async () => {
  const btn = document.getElementById("save");
  btn.disabled = true;
  const labels = {};
  document.querySelectorAll(".card").forEach(c => {
    const it = batch[+c.dataset.i];
    labels[it.url] = {v:"visible", n:"not_visible", u:"unknown"}[c.dataset.s];
  });
  const r = await fetch("/api/labels", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(labels)
  });
  const d = await r.json();
  document.getElementById("msg").textContent =
    `Saved ${d.stored} label(s).`;
  btn.disabled = false;
  await load();
});

document.getElementById("skip").addEventListener("click", load);
load();
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _State:
    def __init__(self, include_labelled):
        self.include_labelled = include_labelled
        self.lock = threading.Lock()
        self.items = build_index(include_labelled)
        self.total = len(self.items) + len(tread.load())

    def batch(self, n=BATCH):
        with self.lock:
            labels = tread.load()
            pending = [i for i in self.items
                       if tread.verdict(i["url"], labels) == "unlabelled"]
            return pending[:n], len(labels), self.total


def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):            # keep the console quiet
            pass

        def _send(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/batch":
                items, labelled, total = state.batch()
                return self._send(200, json.dumps(
                    {"items": items, "labelled": labelled, "total": total}),
                    "application/json")
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/api/labels":
                return self._send(404, "not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            cache = tread.load()
            stored = 0
            for url, label in payload.items():
                if label in tread.LABELS and tread.record(
                        url, label, "human", cache=cache):
                    stored += 1
            tread.save(cache)
            self._send(200, json.dumps({"stored": stored}), "application/json")

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--all", action="store_true",
                    help="Include photos that already carry a label, so an "
                         "earlier machine-made pass can be corrected by hand.")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    print("Indexing tire photos from the current CSVs...")
    state = _State(args.all)
    pending, labelled, total = state.batch()
    if not state.items:
        print("Nothing to review — every tire photo already carries a label.")
        print("Re-run with --all to revisit the ones already labelled.")
        return

    by_sup = collections.Counter(i["supplier"] for i in state.items)
    print(f"{len(state.items)} photo(s) awaiting review "
          f"({labelled} already labelled of {total}).")
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(by_sup.items())))

    url = f"http://127.0.0.1:{args.port}"
    with _Server(("127.0.0.1", args.port), make_handler(state)) as httpd:
        print(f"\n  Review at  {url}\n  Ctrl-C when you are done — "
              f"labels save as you go.\n")
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n" + tread.format_summary())


if __name__ == "__main__":
    main()
