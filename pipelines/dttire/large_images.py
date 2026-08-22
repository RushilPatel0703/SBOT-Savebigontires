#!/usr/bin/env python3
"""Resolve DT Tire's FULL-SIZE product image from the product-detail page.

THE BUG THIS FIXES
------------------
Both DT Tire scrapers read the image straight off the search-results listing:

    img_el = li.select_one(".product-thumbnail")
    image_url = img_el.get("data-src", "")

That element is a thumbnail in the literal sense — every image it yields is
**130x130**. Measured across the 2026-08-18 scrape, that is not a minority
case, it is all of them:

    dttire tires   979 unique images -> 130x130 in every URL shape sampled
    dttire wheels  511 unique images -> 130x130

and those 130px files are what update_images.py PUTs to Shopify as the
storefront photo.

The same product's PDP carries a **600x600** copy of the same shot. The 130
and 600 variants are separate Hybris media objects with different PKs, e.g.

    600: /medias/sys_master/images/h99/hde/11088566091806/600-conversionFormat_base-...
    130: /medias/sys_master/images/h97/he1/11088566157342/130-conversionFormat_base-...
                                        ^^^^^^^^^^^^^^^^ different PK

so the large URL CANNOT be derived from the thumbnail URL by string surgery —
the PK is not a function of the thumbnail's. It has to be read off the PDP,
which is what this module does.

WHY THIS IS CHEAP
-----------------
One image serves every size of a tire model: 8,986 tire rows share only 979
distinct images (514 wheel rows share 511). Callers dedupe by image basename
and resolve once per distinct image, so a full upgrade costs ~1.5k PDP fetches,
not ~9.5k.

PICKING THE RIGHT SHOT
----------------------
The PDP gallery is `.js-gallery-image img.lazy-img` (600px). The sibling
`.js-gallery-carousel img.lazy-thumb` is the 130px strip — selecting the
gallery rather than "every <img> on the page" is also what keeps the brand
logo (a 140x34 image that sits outside the gallery) from being picked.

Within the gallery the views are ordered ISO, FRONT, SIDE. ISO is the angled
three-quarter view showing the tread — the framing the client asked for, and
the one pipelines/shared/image_view.py's face-on check exists to protect. We
match "-ISO-" explicitly rather than trusting position, so a reordered gallery
cannot silently swap in a flat side-on shot.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

BASE = "https://dttirehub.ca"
BASE_URL = f"{BASE}/touchettestorefront/touchette/en"

# 600px gallery. NOT `.js-gallery-carousel img.lazy-thumb` (130px strip) and
# NOT a bare `img` sweep (would catch the manufacturer logo).
GALLERY_SEL = ".js-gallery-image img.lazy-img"

# DT Tire serves this when a product simply has no photo. It is a real, non-
# uniform 300x300 PNG, so a pixel check cannot tell it apart from a genuine
# picture — it has to be matched by name.
PLACEHOLDER_RE = re.compile(r"missing_product_[A-Z]{2}_\d+x\d+\.png", re.IGNORECASE)


def is_placeholder(url):
    """True for DT Tire's 'no photo available' graphic, which must be treated
    as an absent image rather than shipped to the storefront as a picture."""
    return bool(PLACEHOLDER_RE.search(str(url or "")))


def absolutize(url):
    url = str(url or "").strip()
    if url.startswith("/"):
        return BASE + url
    return url


def pdp_url(code, kind):
    """kind is 'tire' or 'wheel' — the PDP route differs between them."""
    search_type = "tireSizeSearch" if kind == "tire" else "wheelSearch"
    return f"{BASE_URL}/p/{kind}/{code}?searchType={search_type}"


def extract_pdp_image(html):
    """Best full-size image URL from a PDP, or '' if the page has none."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for img in soup.select(GALLERY_SEL):
        u = (img.get("data-src") or img.get("src") or "").strip()
        if u and not is_placeholder(u):
            urls.append(u)
    if not urls:
        return ""
    for u in urls:
        if "-ISO-" in u.upper():          # angled tread view — what we want
            return absolutize(u)
    return absolutize(urls[0])


def _sku_of(li):
    """The SKU a search-result item actually belongs to."""
    el = li.select_one(".js-product-sku")
    if el and el.get("data-sku"):
        return str(el.get("data-sku")).strip()
    el = li.select_one("[data-sku]")
    return str(el.get("data-sku")).strip() if el else ""


def find_code(session, sku, kind, timeout=30, retries=3):
    """Look up a product's PDP code (e.g. '80913.MICH') by SKU.

    Only needed when the code was not captured at scrape time — i.e. by
    backfill_images.py against CSVs written before the scrapers recorded it.
    Live scrapes read `data-code` straight off the listing and skip this.

    The SKU search is a PREFIX match, not an exact one, so the first result is
    frequently a different product: searching '10670' (a Nexen tire) returns
    '106703.CEAT', a CEAT tractor tire. Taking result #1 would have written a
    picture of the wrong tire onto the listing — worse than the 130px
    thumbnail it replaced. So every candidate's own data-sku is compared
    against the one asked for, and anything else is refused.

    Transient HTTP 500s are common on this endpoint under concurrency; they
    are retried rather than silently costing the product its upgrade.
    """
    if kind == "tire":
        url = f"{BASE_URL}/tireSearch/tire-size/search"
        params = {"rearTireSize": "", "description": "", "manufacturer": "", "model": "",
                  "modelCode": "", "sku": sku, "season": "ALL",
                  "showOnlyOnSaleResults": "false", "searchQuery": "", "sort": "relevance",
                  "tireSize": ""}
    else:
        url = f"{BASE_URL}/wheelSearch/search"
        params = {"wheelSize": "", "boltPattern": "", "sku": sku, "manufacturer": "",
                  "model": "", "winterApproved": "false", "_showOnlyOnSaleResults": "on",
                  "searchQuery": "", "sort": "relevance"}
    want = str(sku).strip()
    text = ""
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                text = r.text
                break
            if r.status_code < 500:
                return ""                     # a real 4xx will not improve
        except Exception:
            pass
        time.sleep(0.5 * (2 ** attempt))
    if not text:
        return ""

    soup = BeautifulSoup(text, "html.parser")
    for li in soup.select("li.search-results__item"):
        if _sku_of(li) != want:
            continue                          # prefix match on a different product
        el = li.select_one("[data-code]")
        if el and el.get("data-code"):
            return el.get("data-code")
        btn = li.select_one("[data-ajaxurl]")
        if btn:
            m = re.search(r"/p/(?:tire|wheel)/([^?]+)", btn.get("data-ajaxurl", ""))
            if m:
                return m.group(1)
    return ""


def resolve_one(session, code, kind, timeout=30, retries=3):
    """Fetch one PDP and return its full-size image URL ('' on any failure —
    a resolution miss must leave the existing image alone, never blank it).

    Retries 5xx: this endpoint returns transient 500s under concurrency, and
    swallowing them silently costs the product its upgrade for no reason.
    """
    for attempt in range(retries):
        try:
            r = session.get(pdp_url(code, kind), timeout=timeout)
            if r.status_code == 200:
                return extract_pdp_image(r.text)
            if r.status_code < 500:
                return ""
        except Exception:
            pass
        time.sleep(0.5 * (2 ** attempt))
    return ""


# A basename that is a real Hybris filename rather than a base64 ?context= blob.
_REAL_BASENAME_RE = re.compile(r"\.(png|jpe?g|gif|webp)$", re.IGNORECASE)


def basename(url):
    return str(url or "").split("/")[-1].split("?")[0]


# The camera angle inside a filename: 0000009273-SIDE-XL-PIRELLI-...
_VIEW_TOKEN_RE = re.compile(r"-(?:ISO|FRONT|SIDE)-", re.IGNORECASE)

# DT Tire's product-image code, which leads every real filename:
#   0000009273-SIDE-XL-PIRELLI-SCORPION-ATR-NCS.png
#   ^^^^^^^^^^
# This is the identity of the photo set. Everything after it is decoration
# the supplier is inconsistent about — see _image_code().
_IMAGE_CODE_RE = re.compile(r"^(\d{6,})\b")


def _view_agnostic(name):
    """Filename with the view token removed, for comparing two shots of the
    same product taken from different angles."""
    return _VIEW_TOKEN_RE.sub("-", str(name or "")).lower()


def _image_code(name):
    """The leading product-image code, or '' when the filename has none."""
    m = _IMAGE_CODE_RE.match(str(name or ""))
    return m.group(1) if m else ""


def same_product_image(old_url, new_url):
    """Guard against upgrading a row to a DIFFERENT product's photo.

    DT Tire's 130px and 600px variants are separate media objects with
    different PKs and different hash folders, but they share the SAME
    filename:

        600: .../h99/hde/11088566091806/600-conversionFormat_..._Pirelli_Ice_Friction/0000007201-ISO-XL-Pirelli-Ice-Friction.png
        130: .../h97/he1/11088566157342/130-conversionFormat_..._Pirelli_Ice_Friction/0000007201-ISO-XL-Pirelli-Ice-Friction.png

    So whenever the old URL carries a readable filename, the replacement's
    filename must match it exactly. That turns a wrong-product resolution
    into a skipped upgrade instead of a wrong picture on the listing.

    Identity is the LEADING IMAGE CODE, not the whole filename. A filename is
    shaped `{imagecode}-{VIEW}-XL-{PRODUCT NAME}.png`, and everything after
    the code varies between the thumbnail and the full-size copy in ways that
    have nothing to do with which product it is. All of these are the same
    photo set, and comparing whole filenames refused every one of them:

        0000000893-ISO-XL-Goodyear-Eagle-Enforcer-AW-130-px.png
        0000000893-ISO-XL-Goodyear-Eagle-Enforcer-AW-600-px.png   size in name
        0000002669-ISO-XL-GOODYEAR-WRANGLER-DURATRAC-V2-130.png
        0000002669-ISO-XL-GOODYEAR-WRANGLER-DURATRAC-V2-600.png   size in name
        0000001309-ISO-XL-YOKOHAMA-ICEGUARD-G075-thumbnail.png
        0000001309-ISO-XL-YOKOHAMA-ICEGUARD-G075.png              suffix
        0000009086-SIDE-XL-ROVELO-ARCTIC-VISION-LT.png
        0000009086-ISO-XL-ROVELO-ARCTIC-VISION-LT-.png            view + dash

    The code still catches the case this guard exists for: the CEAT tractor
    tire that a prefix-matched SKU search returned for a Nexen product had
    code 0000005034 against the row's 0000004371.

    Filenames without a leading code (a handful, e.g. `KO2-thumnail.png`) fall
    back to comparing the name with the view token removed. URLs served through
    /medias/?context=<base64> have no readable filename at all; those rely on
    the exact-SKU match find_code() already enforces.
    """
    ob = basename(old_url)
    if not _REAL_BASENAME_RE.search(ob):
        return True                      # opaque old URL — nothing to compare
    nb = basename(new_url)
    old_code, new_code = _image_code(ob), _image_code(nb)
    if old_code and new_code:
        return old_code == new_code
    return _view_agnostic(nb) == _view_agnostic(ob)


def resolve_many(session, codes, kind, workers=4, delay=0.15, progress=None):
    """Resolve a list of PDP codes to full-size image URLs.

    Returns {code: url}, omitting codes that could not be resolved. `delay`
    paces each worker so a bulk upgrade stays as polite as the scrapers'
    own DELAY between page fetches.
    """
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    done = 0

    def work(code):
        nonlocal done
        url = resolve_one(session, code, kind)
        time.sleep(delay)
        done += 1
        if progress and done % progress == 0:
            print(f"    ...resolved {done}/{len(codes)}")
        return code, url

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, url in ex.map(work, codes):
            if url:
                out[code] = url
    return out


def upgrade_rows(session, rows, kind, code_key="product_code",
                 image_key="image_url", workers=4, delay=0.15):
    """Upgrade every row's image to the PDP full-size version, in place.

    Deduped by the CURRENT image basename: rows sharing a thumbnail share the
    same underlying photo, so one PDP fetch fixes all of them. Rows whose
    image is DT Tire's missing-product placeholder are blanked, so downstream
    enrichment sees an honest gap instead of a picture of the words "no image".

    Returns (upgraded_row_count, resolved_image_count, placeholder_count).
    """
    groups = {}
    placeholders = 0
    for row in rows:
        cur = str(row.get(image_key, "") or "")
        if is_placeholder(cur):
            row[image_key] = ""
            placeholders += 1
            continue
        code = str(row.get(code_key, "") or "")
        if not code:
            continue
        basename = cur.split("/")[-1].split("?")[0] or code
        groups.setdefault(basename, []).append((code, row))

    reps = [entries[0][0] for entries in groups.values()]
    print(f"  [images] {len(rows)} rows -> {len(groups)} distinct images "
          f"-> {len(reps)} PDP lookups ({placeholders} placeholder(s) blanked)")
    resolved = resolve_many(session, reps, kind, workers=workers, delay=delay,
                            progress=max(50, len(reps) // 10))

    upgraded, mismatched = 0, 0
    for entries in groups.values():
        rep_code = entries[0][0]
        url = resolved.get(rep_code)
        if not url:
            continue
        for _code, row in entries:
            if not same_product_image(row.get(image_key, ""), url):
                mismatched += 1
                continue          # rather no upgrade than the wrong tire
            row[image_key] = url
            upgraded += 1
    print(f"  [images] upgraded {upgraded}/{len(rows)} rows from "
          f"{len(resolved)}/{len(reps)} resolved images"
          + (f"; {mismatched} refused (filename mismatch)" if mismatched else ""))
    return upgraded, len(resolved), placeholders
