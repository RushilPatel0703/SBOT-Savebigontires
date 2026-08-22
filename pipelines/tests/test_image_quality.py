"""Guards on image selection and the image-quality gate.

Three separate defects are pinned here, each one measured against live
supplier data before being fixed (the numbers are in the modules' docstrings):

  1. costar_images  — Costar returns several images for 26% of products, and
     formula's scraper took whichever came LAST regardless of imageType. That
     picked a smaller image than the best available LARGE on 33 of 52
     multi-image products, with SMALL running to a 54px median.
  2. large_images   — DT Tire's listing thumbnail is 130x130 for every
     product; the 600px copy is on the PDP. Resolving it goes through a SKU
     search that PREFIX-matches, so it can land on a different product
     entirely ('10670' -> '106703.CEAT').
  3. enrichment     — "has an image URL" was treated as "has a usable photo",
     which made 130px thumbnails and placeholder graphics invisible to both
     the gap report and the spend estimate.

Every test here is a pure function call. Nothing in this file touches the
network or the filesystem.
"""

import sys
from pathlib import Path

import pytest

_PIPELINES_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_PIPELINES_DIR / "shared"))
sys.path.insert(0, str(_PIPELINES_DIR / "dttire"))

import costar_images as CI  # noqa: E402
import enrichment as E  # noqa: E402
import large_images as LI  # noqa: E402


def _img(url, image_type, guid="g1"):
    return {"productGuid": guid, "url": url, "imageType": image_type}


# ── costar_images ─────────────────────────────────────────────────────────────

class TestCostarImagePick:
    def test_small_listed_last_never_beats_large(self):
        """The exact formula bug: last-entry-wins let a 54px SMALL replace a
        LARGE that was already in hand."""
        assert CI.pick_best_image([_img("big.jpg", "LARGE"),
                                   _img("tiny.jpg", "SMALL")]) == "big.jpg"

    def test_large_wins_from_either_position(self):
        assert CI.pick_best_image([_img("tiny.jpg", "SMALL"),
                                   _img("big.jpg", "LARGE")]) == "big.jpg"

    def test_tie_is_broken_by_first_occurrence_not_last(self):
        """Among equals the choice must be stable across runs, so the same
        scrape twice cannot produce two different storefront photos."""
        entries = [_img("a.jpg", "LARGE"), _img("b.jpg", "LARGE")]
        assert CI.pick_best_image(entries) == "a.jpg"
        assert CI.pick_best_image(list(reversed(entries))) == "b.jpg"

    def test_unlabelled_ranks_between_large_and_small(self):
        assert CI.pick_best_image([_img("u.jpg", ""), _img("s.jpg", "SMALL")]) == "u.jpg"
        assert CI.pick_best_image([_img("u.jpg", ""), _img("l.jpg", "LARGE")]) == "l.jpg"

    def test_image_type_match_is_case_insensitive(self):
        assert CI.pick_best_image([_img("b.jpg", "large"),
                                   _img("t.jpg", "small")]) == "b.jpg"

    def test_entries_without_a_url_are_ignored(self):
        assert CI.pick_best_image([{"productGuid": "g", "imageType": "LARGE"},
                                   _img("real.jpg", "SMALL")]) == "real.jpg"

    def test_imageurl_key_is_accepted(self):
        """formula's payloads use `imageUrl` where northcountry uses `url`."""
        assert CI.pick_best_image(
            [{"productGuid": "g", "imageUrl": "x.jpg", "imageType": "LARGE"}]) == "x.jpg"

    @pytest.mark.parametrize("empty", [[], None])
    def test_no_entries_is_empty_string_not_an_error(self, empty):
        assert CI.pick_best_image(empty) == ""

    def test_build_image_map_picks_per_product(self):
        got = CI.build_image_map([
            _img("s.jpg", "SMALL", "g1"), _img("l.jpg", "LARGE", "g1"),
            _img("only.jpg", "SMALL", "g2"),
        ])
        assert got == {"g1": "l.jpg", "g2": "only.jpg"}

    def test_build_image_map_omits_products_with_no_usable_image(self):
        got = CI.build_image_map([{"productGuid": "g3", "url": "", "imageType": "LARGE"}])
        assert "g3" not in got

    def test_candidate_urls_returns_every_image_deduped_in_order(self):
        assert CI.candidate_urls([_img("a.jpg", "LARGE"), _img("b.jpg", "SMALL"),
                                  _img("a.jpg", "SMALL"),
                                  {"productGuid": "g"}]) == ["a.jpg", "b.jpg"]


def _q(**sizes):
    """Quality cache: name -> short side, or name -> verdict string."""
    out = {}
    for url, v in sizes.items():
        if isinstance(v, str):
            out[url] = {"w": None, "h": None, "verdict": v, "reason": ""}
        else:
            out[url] = {"w": v, "h": v, "verdict": "ok", "reason": ""}
    return out


class TestCostarMeasuredPick:
    """imageType is not trustworthy — see costar_images.py for the measured
    Cooper Endeavor Plus payload where LARGE is 200x290 and SMALL is 400x580."""

    def test_measured_size_beats_a_lying_large_label(self):
        entries = [_img("labelled_large.jpg", "LARGE"),
                   _img("labelled_small.jpg", "SMALL")]
        cache = _q(**{"labelled_large.jpg": 200, "labelled_small.jpg": 400})
        assert CI.pick_best_image(entries, cache) == "labelled_small.jpg"
        # ...and without the cache the old label ranking still applies
        assert CI.pick_best_image(entries) == "labelled_large.jpg"

    def test_label_agrees_with_measurement_most_of_the_time(self):
        entries = [_img("l.jpg", "LARGE"), _img("s.jpg", "SMALL")]
        assert CI.pick_best_image(entries, _q(**{"l.jpg": 600, "s.jpg": 54})) == "l.jpg"

    def test_one_unmeasured_candidate_falls_back_to_label_ranking(self):
        """Half-measured is worse than consistently label-ranked: an unmeasured
        image could be the biggest one and there is no way to know."""
        entries = [_img("l.jpg", "LARGE"), _img("unknown.jpg", "SMALL")]
        assert CI.pick_best_image(entries, _q(**{"l.jpg": 200})) == "l.jpg"

    def test_dead_link_is_dropped_even_when_others_are_unmeasured(self):
        """Knowing an image is dead is enough to exclude it on any evidence."""
        entries = [_img("dead.jpg", "LARGE"), _img("alive.jpg", "SMALL")]
        cache = _q(**{"dead.jpg": "unusable"})
        assert CI.pick_best_image(entries, cache) == "alive.jpg"

    def test_placeholder_is_dropped_the_same_way(self):
        entries = [_img("blank.jpg", "LARGE"), _img("real.jpg", "SMALL")]
        assert CI.pick_best_image(entries, _q(**{"blank.jpg": "placeholder"})) == "real.jpg"

    def test_all_candidates_dead_still_returns_something(self):
        """A bad image beats no image — blanking a listing is not an upgrade."""
        entries = [_img("d1.jpg", "LARGE"), _img("d2.jpg", "SMALL")]
        cache = _q(**{"d1.jpg": "unusable", "d2.jpg": "unusable"})
        assert CI.pick_best_image(entries, cache) == "d1.jpg"

    def test_equal_measurements_fall_back_to_label_then_order(self):
        entries = [_img("s.jpg", "SMALL"), _img("l.jpg", "LARGE")]
        assert CI.pick_best_image(entries, _q(**{"s.jpg": 400, "l.jpg": 400})) == "l.jpg"

    def test_equal_size_and_label_is_stable_across_runs(self):
        entries = [_img("a.jpg", "LARGE"), _img("b.jpg", "LARGE")]
        cache = _q(**{"a.jpg": 400, "b.jpg": 400})
        assert CI.pick_best_image(entries, cache) == "a.jpg"

    def test_empty_cache_behaves_exactly_like_no_cache(self):
        entries = [_img("l.jpg", "LARGE"), _img("s.jpg", "SMALL")]
        assert CI.pick_best_image(entries, {}) == CI.pick_best_image(entries)

    def test_build_image_map_threads_the_cache_through(self):
        got = CI.build_image_map(
            [_img("l.jpg", "LARGE", "g1"), _img("s.jpg", "SMALL", "g1")],
            _q(**{"l.jpg": 200, "s.jpg": 400}))
        assert got == {"g1": "s.jpg"}

    def test_cooper_endeavor_plus_payload(self):
        """The real three-image payload that started this, verbatim."""
        entries = [
            {"productGuid": "cfd2fd46", "imageType": "LARGE", "url": "680e4435.jpg"},
            {"productGuid": "cfd2fd46", "imageType": "SMALL", "url": "670e4435.jpg"},
            {"productGuid": "cfd2fd46", "imageType": "SMALL", "url": "690e4435.jpg"},
        ]
        cache = {"680e4435.jpg": {"w": 200, "h": 290, "verdict": "low_res"},
                 "670e4435.jpg": {"w": 400, "h": 580, "verdict": "low_res"},
                 "690e4435.jpg": {"w": 54, "h": 54, "verdict": "low_res"}}
        assert CI.pick_best_image(entries) == "680e4435.jpg"        # the old bug
        assert CI.pick_best_image(entries, cache) == "670e4435.jpg"  # fixed

    def test_measuring_failure_never_breaks_a_scrape(self, monkeypatch):
        """A scrape must degrade to label ranking, not crash, if Pillow is
        missing or the CDN is unreachable."""
        import enrichment
        monkeypatch.setattr(enrichment, "measure_image_urls",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no PIL")))
        got = CI.measure_and_build_image_map(
            [_img("l.jpg", "LARGE", "g1"), _img("s.jpg", "SMALL", "g1")])
        assert got == {"g1": "l.jpg"}


# ── large_images (DT Tire) ────────────────────────────────────────────────────

_PDP = """
<div class="image-gallery js-gallery">
  <div class="carousel image-gallery__image js-gallery-image">
    <div class="item"><div class="pdp-carousel-img">
      <img class="lazyOwl lazy-img" data-src="/m/600-conversionFormat_x/0000007201-ISO-XL-Pirelli.png">
    </div></div>
    <div class="item"><div class="pdp-carousel-img">
      <img class="lazyOwl lazy-img" data-src="/m/600-conversionFormat_x/0000007201-SIDE-XL-Pirelli.png">
    </div></div>
  </div>
  <div class="carousel gallery-carousel js-gallery-carousel">
    <a class="item"><img class="lazyOwl lazy-thumb"
       data-src="/m/130-conversionFormat_x/0000007201-ISO-XL-Pirelli.png"></a>
  </div>
</div>
<img class="search-results__manufacturer-logo" src="/m/root/logo-pirelli.png">
"""


class TestDtTirePdpExtraction:
    def test_takes_the_600px_gallery_not_the_130px_thumb_strip(self):
        got = LI.extract_pdp_image(_PDP)
        assert "600-conversionFormat" in got
        assert "130-conversionFormat" not in got

    def test_prefers_the_iso_angled_tread_view(self):
        """ISO is the three-quarter view showing tread — the framing the
        client asked for. Matched by name so a reordered gallery cannot
        silently substitute the flat SIDE shot."""
        assert "-ISO-" in LI.extract_pdp_image(_PDP)

    def test_never_returns_the_manufacturer_logo(self):
        assert "logo" not in LI.extract_pdp_image(_PDP).lower()

    def test_absolutizes_a_root_relative_url(self):
        assert LI.extract_pdp_image(_PDP).startswith("https://dttirehub.ca/")

    def test_page_without_a_gallery_yields_nothing(self):
        assert LI.extract_pdp_image("<html><body>no gallery</body></html>") == ""


class TestDtTireWrongProductGuard:
    def test_same_filename_across_variants_is_accepted(self):
        """The 130 and 600 copies differ only in path, never in filename."""
        old = "https://d/x/130-conversionFormat_a/0000004371-ISO-XL-Pirelli.png"
        new = "https://d/y/600-conversionFormat_b/0000004371-ISO-XL-Pirelli.png"
        assert LI.same_product_image(old, new) is True

    def test_a_different_products_photo_is_refused(self):
        """'10670' (Nexen) prefix-matched '106703.CEAT' on the live site. Had
        that been applied it would have put a tractor tire on the listing."""
        old = "https://d/x/0000004371-ISO-XL-Pirelli.png"
        new = "https://d/y/0000005034-ISO-XL-CEAT-FARMAX-R80.png"
        assert LI.same_product_image(old, new) is False

    def test_opaque_context_url_falls_back_to_trusting_the_sku_match(self):
        """A /medias/?context=<base64> URL has no filename to compare."""
        old = "https://d/touchettestorefront/medias/?context=bWFzdGVyfGltYWdlcw"
        new = "https://d/y/600-conversionFormat_b/0000004371-ISO-XL-Pirelli.png"
        assert LI.same_product_image(old, new) is True

    def test_a_different_camera_angle_of_the_same_product_is_allowed(self):
        """The listing thumb is not always the same view as the PDP's ISO
        shot. Comparing whole filenames refused those and cost them the
        upgrade; only the image code and product name have to match."""
        old = "https://d/x/0000009273-SIDE-XL-PIRELLI-SCORPION-ATR-NCS.png"
        new = "https://d/y/0000009273-ISO-XL-PIRELLI-SCORPION-ATR-NCS.png"
        assert LI.same_product_image(old, new) is True

    def test_view_agnostic_matching_still_rejects_a_different_image_code(self):
        """Relaxing the view must not relax the product identity."""
        old = "https://d/x/0000009273-SIDE-XL-PIRELLI-SCORPION-ATR-NCS.png"
        new = "https://d/y/0000005034-ISO-XL-PIRELLI-SCORPION-ATR-NCS.png"
        assert LI.same_product_image(old, new) is False

    @pytest.mark.parametrize("old_name,new_name", [
        # the supplier bakes the pixel size into the filename
        ("0000000893-ISO-XL-Goodyear-Eagle-Enforcer-AW-130-px.png",
         "0000000893-ISO-XL-Goodyear-Eagle-Enforcer-AW-600-px.png"),
        ("0000002669-ISO-XL-GOODYEAR-WRANGLER-DURATRAC-V2-130.png",
         "0000002669-ISO-XL-GOODYEAR-WRANGLER-DURATRAC-V2-600.png"),
        # ...or a '-thumbnail' suffix, or a stray trailing dash
        ("0000001309-ISO-XL-YOKOHAMA-ICEGUARD-G075-thumbnail.png",
         "0000001309-ISO-XL-YOKOHAMA-ICEGUARD-G075.png"),
        ("0000009086-SIDE-XL-ROVELO-ARCTIC-VISION-LT.png",
         "0000009086-ISO-XL-ROVELO-ARCTIC-VISION-LT-.png"),
    ])
    def test_decoration_after_the_image_code_does_not_block_an_upgrade(
            self, old_name, new_name):
        """Real pairs from the catalogue. Everything after the leading image
        code varies between the thumbnail and the full-size copy; only the
        code identifies the product."""
        assert LI.same_product_image(f"https://d/x/{old_name}",
                                     f"https://d/y/{new_name}") is True

    def test_a_different_image_code_is_still_rejected(self):
        """Relaxing the decoration must not relax product identity — this is
        the CEAT-for-Nexen case the guard exists for."""
        old = "https://d/x/0000004371-ISO-XL-Pirelli-P-Zero.png"
        new = "https://d/y/0000005034-ISO-XL-CEAT-FARMAX-R80.png"
        assert LI.same_product_image(old, new) is False

    def test_codeless_filenames_fall_back_to_view_agnostic_name_matching(self):
        assert LI.same_product_image("https://d/x/KO2-thumnail.png",
                                     "https://d/y/KO2-thumnail.png") is True
        assert LI.same_product_image("https://d/x/KO2-thumnail.png",
                                     "https://d/y/OTHER-thing.png") is False


class TestDtTirePlaceholder:
    def test_missing_product_graphic_is_a_placeholder(self):
        assert LI.is_placeholder("https://d/_ui/images/missing_product_EN_300x300.png")

    def test_a_real_photo_is_not(self):
        assert not LI.is_placeholder("https://d/m/0000007201-ISO-XL-Pirelli.png")


# ── enrichment image-quality gate ─────────────────────────────────────────────

class TestImageVerdicts:
    _CACHE = {
        "https://x/small.jpg": {"w": 130, "h": 130, "verdict": "low_res"},
        "https://x/good.jpg":  {"w": 1500, "h": 1500, "verdict": "ok"},
        "https://x/dead.jpg":  {"w": None, "h": None, "verdict": "unusable"},
    }

    @pytest.mark.parametrize("url,expected", [
        ("", "missing"),
        ("https://x/missing_product_EN_300x300.png", "placeholder"),
        ("https://x/small.jpg", "low_res"),
        ("https://x/good.jpg", "ok"),
        ("https://x/dead.jpg", "unusable"),
    ])
    def test_verdicts(self, url, expected):
        assert E.image_verdict(url, self._CACHE) == expected

    def test_a_present_but_tiny_image_counts_as_a_gap(self):
        """The whole point: the column is not blank, and it is still a gap."""
        assert E.image_verdict("https://x/small.jpg", self._CACHE) in E.BAD_IMAGE_VERDICTS

    def test_a_good_image_is_not_a_gap(self):
        assert E.image_verdict("https://x/good.jpg", self._CACHE) not in E.BAD_IMAGE_VERDICTS

    def test_an_unmeasured_image_is_never_guessed_to_be_bad(self):
        """An image nobody has measured is not evidence of a bad image.
        Counting it would inflate the number the client is asked to approve."""
        assert E.image_verdict("https://x/never-seen.jpg", self._CACHE) == "unknown"
        assert "unknown" not in E.BAD_IMAGE_VERDICTS

    def test_placeholder_is_matched_by_name_before_any_cache_lookup(self):
        """A placeholder is a real, non-uniform image; only its name gives it
        away, so the verdict must not depend on having measured it."""
        assert E.image_verdict("https://x/no-image.png", {}) == "placeholder"

    def test_quality_floor_is_below_the_serp_selection_floor(self):
        """Condemning a photo we own is a stricter decision than picking among
        free candidates, so the two thresholds must not be the same number."""
        import image_view
        assert E.IMAGE_MIN_SIDE_PX < image_view.MIN_SIDE_PX


# ── backfill_costar_images: scraper module loading ───────────────────────────

class TestCostarBackfillLoadsTheRightScraper:
    """Three suppliers ship a module called scraper_tires and three ship
    scraper_wheels. A bare `import scraper_tires` returns whichever one landed
    in sys.modules FIRST, so the backfill would query northcountry's host with
    emergetires' GUIDs and get an empty productImageList — indistinguishable
    from "this supplier has no better images". It has to load by file path."""

    def _targets(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).parent.parent / "shared" / "backfill_costar_images.py"
        spec = importlib.util.spec_from_file_location("_bf_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_bare_module_names_really_do_collide(self):
        """If this ever stops being true the unique-name loading is still
        correct, but the comment explaining why would be stale."""
        import collections
        names = [t["module"].split(".")[1] for t in self._targets().TARGETS]
        dupes = [n for n, c in collections.Counter(names).items() if c > 1]
        assert dupes, "expected at least one scraper module name shared by two suppliers"

    def test_every_target_points_at_a_file_that_exists(self):
        from pathlib import Path
        pipelines = Path(__file__).parent.parent
        for t in self._targets().TARGETS:
            pkg, mod = t["module"].split(".")
            assert (pipelines / pkg / f"{mod}.py").exists(), t["module"]

    def test_loader_does_not_use_a_bare_import(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "shared"
               / "backfill_costar_images.py").read_text(encoding="utf-8")
        assert "spec_from_file_location" in src
        assert "__import__(mod)" not in src, (
            "bare __import__ resurfaces the sys.modules collision: the second "
            "supplier's scraper_tires silently becomes the first one's")

    def test_api_shapes_are_the_two_the_loader_handles(self):
        assert {t["api"] for t in self._targets().TARGETS} <= {"api_post", "post"}


# ── the quality cache has to reach the code that SPENDS and WRITES ───────────

class TestQualityCacheIsTheDefault:
    """The audit and the chequebook have to be looking at the same catalogue.

    They were not. `enrich_runner.scan()` passed the measured quality cache
    into `per_source_breakdown()`, so the approval screen counted 5,663 rows
    behind 790 image searches. `run_live()` and `apply_cached()` took
    `quality_cache=None` by default and nobody passed one, so the live run
    could see 57 rows behind 29 searches — and the apply step could not have
    written an answer into any of the other 5,606 rows even if one had been
    bought. Approving the number on the screen would have bought 4% of it.

    Defaulting to the verdicts on disk is what removes the whole class of
    "this caller forgot to pass it", so these tests pin the DEFAULT, not one
    call site.
    """

    CONSUMERS = ["discover_candidates", "per_source_breakdown",
                 "apply_cached", "run_live"]

    @pytest.mark.parametrize("name", CONSUMERS)
    def test_consumer_defaults_to_the_disk_cache(self, name):
        import inspect
        default = inspect.signature(getattr(E, name)).parameters["quality_cache"].default
        assert default is E.USE_DISK_QUALITY_CACHE, (
            f"{name}() defaults to {default!r}: a caller that does not pass a "
            f"quality cache would score images as blank-or-not and silently "
            f"disagree with the number the client approved")

    def test_the_sentinel_reads_the_verdicts_on_disk(self, monkeypatch):
        disk = _q(**{"small.jpg": 130})
        monkeypatch.setattr(E, "load_quality_cache", lambda: disk)
        assert E.resolve_quality_cache("image", E.USE_DISK_QUALITY_CACHE) is disk

    def test_an_explicit_none_still_means_blank_only(self, monkeypatch):
        monkeypatch.setattr(E, "load_quality_cache", lambda: _q(**{"small.jpg": 130}))
        assert E.resolve_quality_cache("image", None) is None

    def test_non_image_fields_never_get_a_quality_cache(self, monkeypatch):
        monkeypatch.setattr(E, "load_quality_cache",
                            lambda: pytest.fail("designation must not read image verdicts"))
        for field in ("designation", "specs"):
            assert E.resolve_quality_cache(field, E.USE_DISK_QUALITY_CACHE) is None

    def test_an_empty_cache_degrades_to_blank_only(self, monkeypatch):
        """No verdicts on disk is not evidence that every image is fine, but
        it is also nothing to score against — it must not crash or invent
        gaps."""
        monkeypatch.setattr(E, "load_quality_cache", dict)
        assert E.resolve_quality_cache("image", E.USE_DISK_QUALITY_CACHE) is None


# ── image cache key: model-level for tires, size-level for wheels ────────────

class TestImageKeyIsPerProductType:
    """Dropping the size from an image key is right for a tire and wrong for a
    wheel, and the 2026-08-18 data says so out loud.

    A tire's "brand + model" names a tread pattern. A wheel's names a category
    or a colour, because that is what the supplier put in the column. Keyed by
    model, `image:rssw|steel wheel|` covered 92 candidate rows across 63
    distinct sizes on one bought photo, and the query behind it was
    "RSSW Steel Wheel wheel product photo" — which identifies no product at
    all. formula's blank brand column produced `image:|black daytona|` (11
    rows) and wheelsmart's produced `image:|gloss black|` (30 distinct
    wheels).

    Keeping size on wheels/rims costs +124 searches (790 -> 914, still far
    inside the 5,000 approval budget).
    """

    def test_tire_sizes_still_share_one_photo(self):
        a = E.cache_key("image", "Cooper", "Endeavor Plus", "245/60R18", "tires")
        b = E.cache_key("image", "Cooper", "Endeavor Plus", "225/65R17", "tires")
        assert a == b == "image:cooper|endeavor plus|"

    @pytest.mark.parametrize("ptype", ["wheels", "rims"])
    def test_wheel_sizes_do_not_share_one_photo(self, ptype):
        a = E.cache_key("image", "RSSW", "Steel Wheel", "18 5-120 (7)", ptype)
        b = E.cache_key("image", "RSSW", "Steel Wheel", "16 5-114.3 (4)", ptype)
        assert a != b
        assert "18 5-120 (7)" in a

    @pytest.mark.parametrize("ptype", ["wheels", "rims"])
    def test_a_wheel_query_carries_the_size_its_key_does(self, ptype):
        """Key and query must agree, or two different searches share one
        cached answer."""
        q = E.build_query("image", "RSSW", "Steel Wheel", "18 5-120 (7)",
                          product_type=ptype)
        assert "18 5-120 (7)" in q

    def test_the_default_product_type_is_the_tire_behaviour(self):
        """Every pre-existing caller passes no product type and means tires."""
        assert (E.cache_key("image", "Cooper", "Endeavor Plus", "245/60R18")
                == "image:cooper|endeavor plus|")

    def test_the_description_only_guard_still_applies_to_tires(self):
        a = E.cache_key("image", "", "", "17X7 6X139.7 CB106", "tires")
        assert a == "image:||17x7 6x139.7 cb106"

    def test_the_master_flag_still_overrides_every_type(self, monkeypatch):
        monkeypatch.setattr(E, "IMAGE_KEY_INCLUDES_SIZE", True)
        assert "245/60r18" in E.cache_key("image", "Cooper", "Endeavor Plus",
                                          "245/60R18", "tires")


class TestScanMeasuresByDefault:
    """An unmeasured image is deliberately not counted as a gap. That rule is
    right and it has a trap: every URL is unmeasured until something measures
    it, and a scrape rewrites the URLs. `--check-images` was opt-in and the
    dashboard runs plain `--scan`, so the image gap this layer exists to
    surface read 57 rows instead of 5,663 from the only screen the client
    uses — and would have gone dark again after every scrape.
    """

    def _runner(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).parent.parent / "shared" / "enrich_runner.py"
        spec = importlib.util.spec_from_file_location("_runner_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_scan_measures_unless_told_not_to(self):
        import inspect
        default = inspect.signature(self._runner().scan).parameters["check_images"].default
        assert default is True, (
            "scan() that does not measure reports near-zero image gaps on "
            "freshly scraped URLs, which is exactly when the gap is largest")

    def test_opting_out_is_still_possible_and_explicit(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "shared"
               / "enrich_runner.py").read_text(encoding="utf-8")
        assert "--no-check-images" in src


# ── free layer: a photo the catalogue already owns ───────────────────────────

def _cand(key, current="", ptype="tires", **kw):
    d = dict(cache_key=key, current_value=current, type=ptype, supplier="nc",
             writable=True)
    d.update(kw)
    return d


class TestPeerImageReuse:
    """Before anything is bought, a gap should be checked against the photos
    this catalogue is already holding.

    The same tire model is carried by several suppliers, and within one
    supplier a model's 245/60R18 often scrapes a clean 1500px photo while its
    225/65R17 gets a 385px one. Both are pictures of the same tread pattern —
    the premise the model-level cache key already rests on. Measured on the
    2026-08-18 data: 556 of the 5,387 flagged tire rows are fillable this way
    and 28 searches disappear, for no SerpAPI credit and no HTTP request.
    """

    def test_a_bigger_peer_photo_replaces_a_thumbnail(self):
        peers = {"image:toyo|opat iii|": (1500, "big.jpg", "northcountry")}
        cache = _q(**{"thumb.jpg": 385, "big.jpg": 1500})
        c = _cand("image:toyo|opat iii|", current="thumb.jpg")
        assert E._peer_upgrade(c, peers, cache) == "big.jpg"

    def test_a_row_with_no_image_at_all_is_filled(self):
        peers = {"image:toyo|opat iii|": (1500, "big.jpg", "northcountry")}
        c = _cand("image:toyo|opat iii|", current="")
        assert E._peer_upgrade(c, peers, _q(**{"big.jpg": 1500})) == "big.jpg"

    def test_a_row_that_already_has_the_bigger_photo_is_left_alone(self):
        """Never sideways and never smaller — the same rule
        backfill_costar_images.py applies."""
        peers = {"image:toyo|opat iii|": (600, "ok.jpg", "dttire")}
        cache = _q(**{"huge.jpg": 2000, "ok.jpg": 600})
        c = _cand("image:toyo|opat iii|", current="huge.jpg")
        assert E._peer_upgrade(c, peers, cache) == ""

    def test_swapping_a_photo_for_itself_is_not_an_upgrade(self):
        peers = {"image:toyo|opat iii|": (1500, "same.jpg", "nc")}
        c = _cand("image:toyo|opat iii|", current="same.jpg")
        assert E._peer_upgrade(c, peers, _q(**{"same.jpg": 1500})) == ""

    def test_a_product_with_no_peer_is_untouched(self):
        c = _cand("image:obscure|model|", current="thumb.jpg")
        assert E._peer_upgrade(c, {}, _q(**{"thumb.jpg": 130})) == ""

    def test_only_ok_verdicts_may_donate(self, monkeypatch, tmp_path):
        """A gap can never be filled from another gap: an index built over a
        catalogue of thumbnails has nothing to give."""
        import pandas as pd
        csv = tmp_path / "cleaned_tires_20260101_000000.csv"
        pd.DataFrame({"brand": ["Toyo", "Toyo"],
                      "model": ["Opat III", "Opat III"],
                      "size": ["245/60R18", "225/65R17"],
                      "tread_design": ["", ""],
                      "image_url": ["small_a.jpg", "small_b.jpg"]}).to_csv(csv, index=False)
        monkeypatch.setattr(E, "SOURCES", [dict(
            supplier="t", type="tires", field="image", glob=str(csv),
            brand_col="brand", model_col="model", size_col="size", extra_col=None,
            desc_col="tread_design", value_col="image_url", writable=True)])
        # Measured, but measured as too small — a real low_res entry carries
        # its dimensions, so this is not the same as "never measured".
        cache = {"small_a.jpg": {"w": 130, "h": 130, "verdict": "low_res", "reason": ""},
                 "small_b.jpg": {"w": 200, "h": 200, "verdict": "low_res", "reason": ""}}
        assert E.build_peer_image_index(cache) == {}

    def test_a_usable_photo_donates_to_every_size_of_its_model(self, monkeypatch, tmp_path):
        import pandas as pd
        csv = tmp_path / "cleaned_tires_20260101_000000.csv"
        pd.DataFrame({"brand": ["Toyo", "Toyo"],
                      "model": ["Opat III", "Opat III"],
                      "size": ["245/60R18", "225/65R17"],
                      "tread_design": ["", ""],
                      "image_url": ["big.jpg", "small.jpg"]}).to_csv(csv, index=False)
        monkeypatch.setattr(E, "SOURCES", [dict(
            supplier="t", type="tires", field="image", glob=str(csv),
            brand_col="brand", model_col="model", size_col="size", extra_col=None,
            desc_col="tread_design", value_col="image_url", writable=True)])
        index = E.build_peer_image_index(_q(**{"big.jpg": 1500, "small.jpg": 200}))
        assert index == {"image:toyo|opat iii|": (1500, "big.jpg", "t")}

    def test_wheels_never_donate(self, monkeypatch, tmp_path):
        """A wheel's 'model' names a colour or a category as often as a
        product — `RSSW / Steel Wheel` covered 92 different wheels. Sharing a
        photo across that key would put one wheel's picture on all of them."""
        import pandas as pd
        csv = tmp_path / "cleaned_wheels_20260101_000000.csv"
        pd.DataFrame({"brand": ["RSSW", "RSSW"],
                      "model": ["Steel Wheel", "Steel Wheel"],
                      "size": ["18 5-120", "16 5-114.3"],
                      "description": ["", ""],
                      "image_url": ["big.jpg", "small.jpg"]}).to_csv(csv, index=False)
        monkeypatch.setattr(E, "SOURCES", [dict(
            supplier="t", type="wheels", field="image", glob=str(csv),
            brand_col="brand", model_col="model", size_col="size", extra_col=None,
            desc_col="description", value_col="image_url", writable=True)])
        assert E.build_peer_image_index(_q(**{"big.jpg": 1500, "small.jpg": 200})) == {}

    def test_no_verdicts_means_no_donors_rather_than_a_crash(self):
        assert E.build_peer_image_index(None) == {}
        assert E.build_peer_image_index({}) == {}

    def test_apply_writes_the_peer_photo_into_the_csv(self, monkeypatch, tmp_path):
        """End to end, on a real file: the thumbnail row is upgraded, the row
        that already had the good photo is untouched, and nothing is blanked."""
        import pandas as pd
        csv = tmp_path / "cleaned_tires_20260101_000000.csv"
        pd.DataFrame({"brand": ["Toyo", "Toyo", "Toyo"],
                      "model": ["Opat III", "Opat III", "Celsius II"],
                      "size": ["245/60R18", "225/65R17", "205/55R16"],
                      "tread_design": ["", "", ""],
                      "image_url": ["big.jpg", "small.jpg", "orphan.jpg"]}).to_csv(
                          csv, index=False)
        monkeypatch.setattr(E, "SOURCES", [dict(
            supplier="t", type="tires", field="image", glob=str(csv),
            brand_col="brand", model_col="model", size_col="size", extra_col=None,
            desc_col="tread_design", value_col="image_url", writable=True)])
        cache = {"big.jpg": {"w": 1500, "h": 1500, "verdict": "ok", "reason": ""},
                 "small.jpg": {"w": 200, "h": 200, "verdict": "low_res", "reason": ""},
                 "orphan.jpg": {"w": 200, "h": 200, "verdict": "low_res", "reason": ""}}
        monkeypatch.setattr(E, "load_quality_cache", lambda: cache)
        monkeypatch.setattr(E, "load_cache", dict)

        stats = E.apply_cached(["image"], write=True)
        assert stats["image"]["peer_fixed"] == 1
        assert stats["image"]["still_needs_paid"] == 1        # the orphan model

        after = pd.read_csv(csv, dtype=str).fillna("")["image_url"].tolist()
        assert after == ["big.jpg", "big.jpg", "orphan.jpg"], (
            "the thumbnail should take its model's usable photo; the orphan "
            "keeps its small-but-real image rather than being blanked")
