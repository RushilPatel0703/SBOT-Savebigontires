"""Guards on the two things that decide how much SerpAPI credit a run spends.

  1. THE PRODUCT KEY is the catalogue's memory. It has to name a PRODUCT, not
     a spelling. Measured on the 2026-08-26 catalogue, one tread pattern
     arrived under up to four spellings and each bought its own search:

         'p zero'  'p-zero'  'p zero  - ultra high performance'  'pzero'
         'wildpeak a/t4w'  'wildpeak at4w'  'wildpeak a/t4w  - all terrain'

     Normalizing the key removed 178 duplicate image searches outright, and
     unblocked the free peer layer, which had been matching ZERO rows because
     North Country's spelling never equalled DT Tire's. With it, 2,123 North
     Country rows fill from photos the catalogue already owns.

  2. THE TREAD LABEL decides whether a photo the client can already see counts
     as a gap. It must default to "not a gap" while unreviewed, or the first
     run after this lands would queue the entire catalogue for re-purchase.

Every test here is a pure function call against a temp file. Nothing touches
the network, the real caches, or the supplier CSVs.
"""

import json
import sys
from pathlib import Path

import pytest

_PIPELINES_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_PIPELINES_DIR / "shared"))

import enrichment as E  # noqa: E402
import tread  # noqa: E402


class TestProductKeyNormalization:

    @pytest.mark.parametrize("model", [
        "P Zero", "p-zero", "P Zero  - Ultra High Performance", "PZero",
    ])
    def test_spelling_variants_share_one_key(self, model):
        assert (E.cache_key("image", "Pirelli", model, "245/40R18", "tires")
                == "image:pirelli|pzero|")

    def test_the_category_suffix_is_dropped(self):
        a = E.cache_key("image", "Toyo", "Open Country M/T", "35X12.50R20", "tires")
        b = E.cache_key("image", "Toyo", "Open Country M/T  - Mud Terrain",
                        "35X12.50R20", "tires")
        assert a == b == "image:toyo|opencountrymt|"

    def test_a_trailing_bare_dash_is_dropped(self):
        """dttire emits 'Open Country M/T -' with nothing after the dash."""
        assert (E.cache_key("image", "Toyo", "Open Country M/T -", "", "tires")
                == "image:toyo|opencountrymt|")

    def test_a_dash_glued_to_the_next_word_is_not_a_category(self):
        """Costar punctuates its taxonomy '<model>  - <Category>', always.
        A dash glued to the following word came from somewhere else: North
        Country derives models from description text, and
        'P-ZERO (PZ4)-SPORT - 107Y XL' yields 'P-ZERO -SPORT'. That is
        Pirelli's P Zero PZ4 Sport, a different tire from the P Zero — merging
        them would put one tread pattern's photo on the other's listing to
        save a single search."""
        sport = E.cache_key("image", "Pirelli", "P-ZERO -SPORT", "315/30R22", "tires")
        plain = E.cache_key("image", "Pirelli", "P ZERO", "245/35R20", "tires")
        assert sport == "image:pirelli|pzerosport|"
        assert sport != plain

    def test_a_hyphen_inside_a_model_name_is_kept(self):
        """'Observe GSI-6' still merges with its category variant, and the
        internal hyphen is not mistaken for a suffix marker."""
        a = E.cache_key("image", "Toyo", "Observe GSI-6", "", "tires")
        b = E.cache_key("image", "Toyo", "Observe GSI-6  - Ice and snow", "", "tires")
        assert a == b == "image:toyo|observegsi6|"

    def test_two_different_models_still_get_two_keys(self):
        a = E.cache_key("image", "Michelin", "Defender LTX", "", "tires")
        b = E.cache_key("image", "Michelin", "Defender 2", "", "tires")
        assert a != b

    def test_the_suffix_strip_never_eats_the_whole_model(self):
        """A model that IS a category must not normalize to '' and collapse
        every such row in the catalogue onto a single key."""
        assert E.normalize_key_token("- Touring") != ""

    def test_wheel_fitments_are_not_normalized(self):
        """Brand and model are normalized; size is not. Stripping separators
        from a fitment would merge genuinely different wheels."""
        a = E.cache_key("image", "RSSW", "Steel Wheel", "18 5-114.3", "wheels")
        b = E.cache_key("image", "RSSW", "Steel Wheel", "185 114.3", "wheels")
        assert a != b

    def test_the_blank_brand_and_model_guard_still_holds(self):
        """wheelsmart rows with no brand or model keep their size, or every
        one of them would collapse onto 'image:||'."""
        assert (E.cache_key("image", "", "", "17X7 6X139.7 CB106", "tires")
                == "image:||17x7 6x139.7 cb106")


class TestCacheKeyMigration:
    """Tightening the key must never discard an answer already paid for —
    that would be the most expensive possible way to land an optimisation."""

    def _cache(self, monkeypatch, tmp_path, data):
        f = tmp_path / "serp_cache.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(E, "CACHE_FILE", str(f))
        return f

    def test_entries_are_rekeyed_in_place(self, monkeypatch, tmp_path):
        self._cache(monkeypatch, tmp_path,
                    {"image:pirelli|p zero|": {"image": "a.jpg", "query": "q"}})
        assert E.migrate_cache_keys() == (1, 0)
        assert E.load_cache() == {
            "image:pirelli|pzero|": {"image": "a.jpg", "query": "q"}}

    def test_a_real_answer_beats_an_empty_one_when_two_keys_collapse(
            self, monkeypatch, tmp_path):
        self._cache(monkeypatch, tmp_path, {
            "image:pirelli|p zero|": {"image": "", "query": "q1"},
            "image:pirelli|p-zero|": {"image": "found.jpg", "query": "q2"},
        })
        E.migrate_cache_keys()
        assert E.load_cache()["image:pirelli|pzero|"]["image"] == "found.jpg"

    def test_partial_spec_answers_are_merged_not_dropped(self, monkeypatch, tmp_path):
        """Every sub-field in both entries was billed for separately."""
        self._cache(monkeypatch, tmp_path, {
            "specs:toyo|open country|265/70r17": {
                "specs": {"load_index": "115"}, "tried": ["load_index"]},
            "specs:toyo|open country  - mud terrain|265/70r17": {
                "specs": {"speed_rating": "T"}, "tried": ["speed_rating"]},
        })
        E.migrate_cache_keys()
        entry = E.load_cache()["specs:toyo|opencountry|265/70r17"]
        assert entry["specs"] == {"load_index": "115", "speed_rating": "T"}
        assert entry["tried"] == ["load_index", "speed_rating"]

    def test_it_is_idempotent(self, monkeypatch, tmp_path):
        self._cache(monkeypatch, tmp_path,
                    {"image:pirelli|p zero|": {"image": "a.jpg"}})
        E.migrate_cache_keys()
        assert E.migrate_cache_keys() == (0, 0)

    def test_the_formula_migration_does_not_fight_the_rekey(self, monkeypatch,
                                                            tmp_path):
        """formula's 253-entry legacy cache is keyed in the OLD spelling. If it
        is folded in by raw string interpolation it re-injects an un-normalized
        key on every run, which migrate_cache_keys() then folds straight back —
        250 entries of pointless churn per report, and a re-key message that
        makes a healthy cache look broken."""
        old = tmp_path / "formula_serp_cache.json"
        old.write_text(json.dumps(
            {"pirelli|p zero|245/40r18": {"designation": "Summer", "query": "q"}}),
            encoding="utf-8")
        monkeypatch.setattr(E, "FORMULA_OLD_CACHE", str(old))
        self._cache(monkeypatch, tmp_path, {})

        assert E.migrate_formula_cache() == (1, 0)
        assert list(E.load_cache()) == ["designation:pirelli|pzero|245/40r18"]
        # Second pass must recognise its own work.
        assert E.migrate_formula_cache() == (0, 1)
        assert E.migrate_cache_keys() == (0, 0)

    def test_a_model_containing_a_colon_survives(self, monkeypatch, tmp_path):
        """Wheel models read 'CB:124.9 OS:131'. The field comes off the FIRST
        colon and the size off the LAST pipe, or the key tears at the wrong
        place and the entry is silently orphaned."""
        self._cache(monkeypatch, tmp_path, {
            "image:rtx|grey dual wheel cb:124.9 os:131|16x6": {"image": "w.jpg"}})
        E.migrate_cache_keys()
        keys = list(E.load_cache())
        assert len(keys) == 1
        assert keys[0].startswith("image:rtx|") and keys[0].endswith("|16x6")


class TestTreadMemory:
    """A photo showing the side of the tire is a gap — once someone says so."""

    QUALITY_OK = {"a.jpg": {"w": 1500, "h": 1500, "verdict": "ok"}}

    def test_an_unreviewed_photo_is_not_a_gap(self):
        """The client is not billed for a search on the strength of a question
        nobody has answered. This is what keeps the first run after this lands
        from queueing the whole catalogue."""
        assert E.image_verdict("a.jpg", self.QUALITY_OK, {}) == "ok"

    def test_a_rejected_photo_becomes_a_gap(self):
        t = {"a.jpg": {"tread": "not_visible", "source": "human"}}
        assert E.image_verdict("a.jpg", self.QUALITY_OK, t) == "no_tread"
        assert "no_tread" in E.BAD_IMAGE_VERDICTS

    def test_a_confirmed_photo_stays_ok(self):
        t = {"a.jpg": {"tread": "visible", "source": "human"}}
        assert E.image_verdict("a.jpg", self.QUALITY_OK, t) == "ok"

    def test_resolution_is_reported_before_pose(self):
        """A 130px photo is a gap whichever way it faces, and calling it
        'low_res' names the bar a replacement actually has to clear."""
        q = {"a.jpg": {"w": 130, "h": 130, "verdict": "low_res"}}
        t = {"a.jpg": {"tread": "not_visible", "source": "human"}}
        assert E.image_verdict("a.jpg", q, t) == "low_res"

    def test_omitting_the_tread_cache_preserves_the_old_behaviour(self):
        """Every pre-existing caller passes two arguments and means 'ignore
        pose'."""
        assert E.image_verdict("a.jpg", self.QUALITY_OK) == "ok"

    def test_a_human_label_is_not_overwritten_by_a_machine(self, tmp_path):
        p = str(tmp_path / "t.json")
        assert tread.record("a.jpg", "visible", "human", path=p)
        assert not tread.record("a.jpg", "not_visible", "model", path=p)
        assert tread.verdict("a.jpg", tread.load(p)) == "visible"

    def test_a_human_label_overrides_a_heuristic_one(self, tmp_path):
        p = str(tmp_path / "t.json")
        tread.record("a.jpg", "not_visible", "heuristic", path=p)
        assert tread.record("a.jpg", "visible", "human", path=p)
        assert tread.verdict("a.jpg", tread.load(p)) == "visible"

    def test_a_label_is_stored_with_its_provenance_and_time(self, tmp_path):
        p = str(tmp_path / "t.json")
        tread.record("a.jpg", "not_visible", "human", path=p)
        entry = tread.load(p)["a.jpg"]
        assert entry["tread"] == "not_visible"
        assert entry["source"] == "human"
        assert entry["ts"]

    def test_unlabelled_is_distinct_from_unknown(self, tmp_path):
        """'nobody looked' and 'a reviewer looked and could not tell' are
        different facts, and only one of them is worth re-queueing."""
        p = str(tmp_path / "t.json")
        tread.record("b.jpg", "unknown", "human", path=p)
        cache = tread.load(p)
        assert tread.verdict("a.jpg", cache) == "unlabelled"
        assert tread.verdict("b.jpg", cache) == "unknown"

    def test_an_invalid_label_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            tread.record("a.jpg", "maybe", "human", path=str(tmp_path / "t.json"))

    def test_reviewer_shorthand_is_accepted_on_import(self, tmp_path):
        csv_path = tmp_path / "labels.csv"
        csv_path.write_text(
            "idx,label,url\n0,v,a.jpg\n1,donut,b.jpg\n2,,c.jpg\n3,?,d.jpg\n",
            encoding="utf-8")
        p = str(tmp_path / "t.json")
        assert tread.import_labels(str(csv_path), path=p) == (3, 1, 0)
        cache = tread.load(p)
        assert tread.verdict("a.jpg", cache) == "visible"
        assert tread.verdict("b.jpg", cache) == "not_visible"
        assert tread.verdict("c.jpg", cache) == "unlabelled"
        assert tread.verdict("d.jpg", cache) == "unknown"

    def test_a_partly_filled_sheet_can_be_imported_then_finished(self, tmp_path):
        p = str(tmp_path / "t.json")
        first = tmp_path / "a.csv"
        first.write_text("idx,label,url\n0,v,a.jpg\n1,,b.jpg\n", encoding="utf-8")
        tread.import_labels(str(first), path=p)
        second = tmp_path / "b.csv"
        second.write_text("idx,label,url\n1,n,b.jpg\n", encoding="utf-8")
        tread.import_labels(str(second), path=p)
        cache = tread.load(p)
        assert tread.verdict("a.jpg", cache) == "visible"
        assert tread.verdict("b.jpg", cache) == "not_visible"

    def test_a_rejected_photo_is_disqualified_as_a_free_donor(self, tmp_path,
                                                              monkeypatch):
        """Copying a sidewall shot onto 66 other rows would spread the exact
        defect the client asked to remove — for free, which is worse than the
        thumbnail it replaces."""
        import pandas as pd
        csv = tmp_path / "cleaned_tires_20260101_000000.csv"
        pd.DataFrame({"brand": ["Toyo"], "model": ["Opat III"],
                      "size": ["245/60R18"], "tread_design": [""],
                      "image_url": ["side.jpg"]}).to_csv(csv, index=False)
        monkeypatch.setattr(E, "SOURCES", [dict(
            supplier="t", type="tires", field="image", glob=str(csv),
            brand_col="brand", model_col="model", size_col="size", extra_col=None,
            desc_col="tread_design", value_col="image_url", writable=True)])
        quality = {"side.jpg": {"w": 1500, "h": 1500, "verdict": "ok"}}

        monkeypatch.setattr(E._TREAD, "load", lambda *a, **k: {})
        assert E.build_peer_image_index(quality)

        monkeypatch.setattr(E._TREAD, "load", lambda *a, **k: {
            "side.jpg": {"tread": "not_visible", "source": "human"}})
        assert E.build_peer_image_index(quality) == {}


class TestWheelSmartDescriptionParsing:
    """WheelSmart's tire feed hides brand and model inside one free-text
    description. Unparsed, the cache key falls back to the SKU, which defeats
    the model-level image key every other supplier gets: on the 2026-08-26
    catalogue that was 178 searches where 53 cover the same products, and the
    query itself led with 11 digits of SKU.
    """

    def test_brand_and_model_split_off_the_size_and_load_speed(self):
        assert E._parse_wheelsmart_tire_desc(
            "235/55R20 102V DELINTE DS2 SUV ALL SEASON...") == ("DELINTE", "DS2 SUV")

    def test_category_tail_is_dropped_but_size_never_reaches_the_model(self):
        brand, model = E._parse_wheelsmart_tire_desc(
            "205/60R15 91H LANDSAIL CLX-10 ALL SEASON...")
        assert (brand, model) == ("LANDSAIL", "CLX-10")

    def test_ply_rating_and_dash_separator_are_stripped(self):
        assert E._parse_wheelsmart_tire_desc(
            "225/65R16C 112/110T - 8PR LANVIGATOR MILE MAX ALL SEASON..."
        ) == ("LANVIGATOR", "MILE MAX")

    def test_flotation_size_is_recognised_as_a_size(self):
        brand, model = E._parse_wheelsmart_tire_desc(
            "LT33X12.50R17 120Q ROCK525 ROCKBLADE MUD TERRAIN...")
        assert brand == "ROCKBLADE"
        assert "33X12.50R17" not in model

    def test_two_word_brand_is_not_split_across_brand_and_model(self):
        assert E._parse_wheelsmart_tire_desc(
            "225/60R16 98H GROUND SPEED VOYAGER 777EV ALL SEASON..."
        ) == ("GROUND SPEED", "VOYAGER 777EV")

    def test_misspelled_brand_folds_onto_the_canonical_key(self):
        """'LANVIGTOR' is a live typo in the feed. If it does not fold, it
        buys its own search for a product already photographed."""
        good = E._parse_wheelsmart_tire_desc(
            "225/55R18 102VXL LANVIGATOR CATCHFORS ALL WEATHER...")
        typo = E._parse_wheelsmart_tire_desc(
            "205/50R17 93WXL LANVIGTOR CATCHFORS ALL WEATHER...")
        assert good == typo == ("LANVIGATOR", "CATCHFORS")
        assert (E.cache_key("image", *good, "205/50R17", "tires")
                == E.cache_key("image", *typo, "225/55R18", "tires"))

    def test_model_suffix_that_names_a_real_variant_is_kept(self):
        """Lanvigator ships both a CATCHFORS and a CATCHFORS A/S. Stripping
        'A/S' as if it were a category would put one photo on two products."""
        plain = E._parse_wheelsmart_tire_desc(
            "225/55R18 102VXL LANVIGATOR CATCHFORS ALL WEATHER...")
        variant = E._parse_wheelsmart_tire_desc(
            "225/55R18 102VXL LANVIGATOR CATCHFORS A/S ALL WEATHER...")
        assert plain != variant
        assert (E.cache_key("image", *plain, "225/55R18", "tires")
                != E.cache_key("image", *variant, "225/55R18", "tires"))

    def test_one_model_in_many_sizes_collapses_to_one_search(self):
        keys = {
            E.cache_key("image", *E._parse_wheelsmart_tire_desc(d), "", "tires")
            for d in ("235/55R20 102V DELINTE DS2 SUV ALL SEASON...",
                      "235/65R18 110HXL DELINTE DS2 SUV ALL SEASON...",
                      "215/70R16 100H DELINTE DS2 SUV ALL SEASON...")
        }
        assert len(keys) == 1

    def test_unrecognised_brand_returns_nothing_rather_than_guessing(self):
        """A row this parser cannot read must fall through to the old
        behaviour — skipped — not to a query built from half a SKU."""
        assert E._parse_wheelsmart_tire_desc("6 1/2JX15 5-108-45 STEEL WHEEL") == ("", "")
        assert E._parse_wheelsmart_tire_desc("") == ("", "")

    def test_sources_without_a_parser_are_untouched(self):
        """Only the one feed that needs it declares desc_parser; every other
        source must keep resolving brand/model from its real columns."""
        src = {"brand_col": "brand", "model_col": "model",
               "extra_col": None, "desc_col": None}
        row = {"brand": "Cooper", "model": "Endeavor"}
        assert E._resolve_brand_model(row, src) == ("Cooper", "Endeavor")


class TestPatternOnlyModelsAreNotBought:
    """A few DT Tire rows put the tread PATTERN in the model column. The brand
    still reads as meaningful, so without a guard the run buys
    'Bridgestone Asymmetrical tire product photo' and the storefront shows a
    confidently wrong tire.
    """

    @pytest.mark.parametrize("model", ["Directional", "asymmetrical", "SYMMETRICAL"])
    def test_pattern_word_alone_is_not_queryable(self, model):
        assert not E._is_queryable("Bridgestone", model, "")

    def test_a_real_short_model_is_still_queryable(self):
        """The general rule this deny-list replaces would have dropped these."""
        for brand, model in [("Starfire", "WR"), ("Amp", "Terrain Pro A/T"),
                             ("American Truxx", "Dna"), ("Rolling Big Power", "M/T II")]:
            assert E._is_queryable(brand, model, ""), (brand, model)

    def test_pattern_word_with_real_description_still_searches(self):
        """The row is only unbuyable when the pattern word is all there is."""
        assert E._is_queryable("Bridgestone", "Directional", "Potenza Sport summer tire")


class TestSidewallShotsSortLast:
    """The client's one hard requirement on tire photos is that the tread
    faces the camera. image_view's pixel test cannot decide that (tread.py
    records the measurement), and a live run was observed accepting
    '.../951-sidewall/general-altimax-arctic.png' after demoting two other
    candidates. Retail CDNs name the pose in the path, so rank on that.
    """

    @staticmethod
    def _cand(url, title="Michelin Defender product photo"):
        return {"original": url, "original_width": 1200, "original_height": 1200,
                "title": title, "source": "simpletire.com"}

    def test_sidewall_url_ranks_below_an_otherwise_equal_photo(self):
        side = self._cand("https://images.simpletire.com/line-images/951-sidewall/x.png")
        plain = self._cand("https://images.simpletire.com/line-images/951/x.png")
        ranked = E.filter_image_candidates([side, plain], "Michelin", "Defender")
        assert [c["original"] for c in ranked] == [plain["original"], side["original"]]

    def test_sidewall_photo_is_still_bought_when_it_is_the_only_one(self):
        """Demotion, not rejection — a wrong-pose photo the reviewer can throw
        out beats a blank listing, and a hard reject would turn one problem
        into the other silently."""
        side = self._cand("https://images.simpletire.com/line-images/951-sidewall/x.png")
        assert len(E.filter_image_candidates([side], "Michelin", "Defender")) == 1

    def test_sidetread_is_not_read_as_sidewall(self):
        """'sidetread' contains 'side' but means the tread is in frame."""
        st = self._cand("https://images.simpletire.com/line-images/18862-sidetread.png")
        plain = self._cand("https://images.simpletire.com/line-images/18862/x.png")
        ranked = E.filter_image_candidates([st, plain], "Michelin", "Defender")
        assert set(c["original"] for c in ranked) == {st["original"], plain["original"]}
        # neither is penalised, so the tie breaks on pixel area, not on pose
        assert len(ranked) == 2

    def test_penalty_outweighs_a_manufacturer_domain_bonus(self):
        """A sidewall shot on a preferred domain must still lose to a
        tread shot on a worse one, or the bias undoes the pose ranking."""
        side = {"original": "https://www.michelinman.com/img/tire-sidewall.png",
                "original_width": 1200, "original_height": 1200,
                "title": "Michelin Defender", "source": "michelinman.com"}
        plain = self._cand("https://images.simpletire.com/line-images/951/x.png")
        ranked = E.filter_image_candidates([side, plain], "Michelin", "Defender")
        assert ranked[0]["original"] == plain["original"]


class TestSidewallIsTheLastResortTier:
    """pick_verified_image ranks three tiers: accepted > face_on-demoted >
    URL-declared sidewall. Ordering the input list was not enough — a passing
    candidate always beat a demoted one, so the sidewall shot the live run
    accepted would still have won after two good candidates were demoted.
    """

    @staticmethod
    def _c(url):
        return {"original": url, "original_width": 1200, "original_height": 1200,
                "title": "Michelin Defender", "source": "simpletire.com"}

    def _pick(self, urls, findings):
        import image_view as IV
        calls = {}

        def fake_fetch(url, **kw):
            return b"x" + url.encode()

        def fake_analyze(data, product_type="tires"):
            url = data[1:].decode()
            calls[url] = True
            return findings[url]

        orig = IV.analyze_image
        IV.analyze_image = fake_analyze
        try:
            return IV.pick_verified_image([self._c(u) for u in urls],
                                          fetcher=fake_fetch)
        finally:
            IV.analyze_image = orig

    @staticmethod
    def _ok(face_on):
        return {"ok": True, "reasons": [], "width": 1200, "height": 1200,
                "core_bg_frac": 0.5 if face_on else 0.01, "face_on": face_on}

    def test_face_on_demotion_beats_a_url_declared_sidewall(self):
        good = "https://cdn.example.com/tire-a.png"
        side = "https://cdn.example.com/951-sidewall/tire-b.png"
        winner, log = self._pick(
            [good, side], {good: self._ok(True), side: self._ok(False)})
        assert winner["original"] == good, "a pixel guess must not outrank the publisher's label"
        assert any("sidewall" in e["status"] for e in log)

    def test_a_clean_photo_still_wins_outright(self):
        good = "https://cdn.example.com/tire-a.png"
        side = "https://cdn.example.com/951-sidewall/tire-b.png"
        winner, _ = self._pick(
            [good, side], {good: self._ok(False), side: self._ok(False)})
        assert winner["original"] == good

    def test_sidewall_is_returned_when_it_is_all_there_is(self):
        side = "https://cdn.example.com/951-sidewall/tire-b.png"
        winner, _ = self._pick([side], {side: self._ok(False)})
        assert winner["original"] == side, "a wrong-pose photo still beats a blank listing"

    def test_strict_mode_drops_every_questionable_pose(self):
        import image_view as IV
        side = "https://cdn.example.com/951-sidewall/tire-b.png"
        orig = IV.analyze_image
        IV.analyze_image = lambda d, product_type="tires": self._ok(False)
        try:
            winner, _ = IV.pick_verified_image(
                [self._c(side)], strict=True, fetcher=lambda u, **k: b"x")
        finally:
            IV.analyze_image = orig
        assert winner is None


class TestLoadIndexPlausibility:
    """A searched load index is the one enriched value that is worse wrong
    than missing — it reads as a real measurement on a load-bearing part. A
    live run returned load index 70 (335 kg) for a Continental CHD3
    245/70R19.5 commercial truck tire, which carries 2,180 kg at index 135.
    """

    @pytest.fixture(autouse=True)
    def _ref(self, monkeypatch):
        monkeypatch.setattr(E, "_load_index_ref", {
            "24570R19.5": 135,      # commercial truck
            "33X12.50R20": 119,     # light truck, E load range
            "20555R16": 91,         # passenger
        })

    def test_a_passenger_index_on_a_truck_tire_is_rejected(self):
        assert E.implausible_load_index("245/70R19.5", "70")

    def test_the_correct_index_passes(self):
        assert not E.implausible_load_index("245/70R19.5", "135")

    def test_load_range_variation_is_not_treated_as_an_error(self):
        """33X12.50R20 is 114 at load range E and 121 at F. Both are real, so
        a rule tight enough to catch those would throw away good data."""
        for li in ("114", "119", "121", "127"):
            assert not E.implausible_load_index("33X12.50R20", li), li

    def test_an_unknown_size_is_never_called_implausible(self):
        """No reference is not evidence of a bad answer."""
        assert not E.implausible_load_index("777/77R77", "70")

    def test_spacing_and_punctuation_do_not_defeat_the_lookup(self):
        assert E.implausible_load_index("245/70 R19.5", "70")
        assert E.implausible_load_index("245 / 70R19.5", "70")

    def test_service_type_prefix_is_part_of_the_size_identity(self):
        """LT and P-metric sizes of the same nominal dimensions carry
        different load indices, so they must not share a reference. Both
        sides of the lookup keep the prefix, so they agree; a size the
        reference has never seen simply goes unjudged, which is the safe
        direction."""
        assert not E.implausible_load_index("LT245/70R19.5", "70")

    def test_only_low_values_are_rejected(self):
        """An unexpectedly high index is usually a heavier load range of the
        same tire, not a parse error."""
        assert not E.implausible_load_index("20555R16", "125")

    def test_non_numeric_is_left_alone(self):
        assert not E.implausible_load_index("245/70R19.5", "")
        assert not E.implausible_load_index("245/70R19.5", "XL")


class TestSharedImageAudit:
    """The mirror of the key-normalization audit. That one catches two
    spellings of one product collapsing onto one key; this catches two
    different products ending up with one picture. On the 2026-08-30 live run
    a Shopify *collection* banner won 39 keys across 5 brands, and a lug-nut
    "Install Kit" photo won 7 more.
    """

    def _cache(self):
        return {
            # one model, many sizes — normal and wanted, must NOT be flagged
            "image:nexen|winguard|86t": {"image": "https://cdn/nexen-winguard.png"},
            "image:nexen|winguard|88t": {"image": "https://cdn/nexen-winguard.png"},
            # one picture across brands — wrong
            "image:touren|tr79|18x8": {"image": "https://cdn/collections/banner.jpg"},
            "image:envy|envy101|16x8": {"image": "https://cdn/collections/banner.jpg"},
            "image:rssw|blitz|17x7":   {"image": "https://cdn/collections/banner.jpg"},
            # an empty answer is not a shared photo
            "image:acme|nothing|":     {"image": ""},
        }

    def test_one_model_across_sizes_is_not_flagged(self):
        assert "https://cdn/nexen-winguard.png" not in E.audit_shared_images(self._cache())

    def test_one_photo_across_brands_is_flagged(self):
        out = E.audit_shared_images(self._cache())
        assert list(out) == ["https://cdn/collections/banner.jpg"]
        assert len(out["https://cdn/collections/banner.jpg"]) == 3

    def test_quarantine_blanks_the_answer_but_keeps_the_key(self):
        """Deleting the key would re-queue it and the next run would pay to
        make the same mistake."""
        cache = self._cache()
        _, touched = E.quarantine_shared_images(cache, write=False)
        assert touched == 3
        for k in ("image:touren|tr79|18x8", "image:envy|envy101|16x8"):
            assert k in cache
            assert cache[k]["image"] == ""
            assert cache[k]["quarantined"]["shared_with"] == 3
        # the legitimate shared photo is untouched
        assert cache["image:nexen|winguard|86t"]["image"] == "https://cdn/nexen-winguard.png"

    def test_quarantine_is_idempotent(self):
        cache = self._cache()
        E.quarantine_shared_images(cache, write=False)
        _, second = E.quarantine_shared_images(cache, write=False)
        assert second == 0

    def test_a_quarantined_key_is_not_re_queued_for_spend(self):
        """run_live skips any key already present in the cache, so a blanked
        entry must still BE an entry."""
        cache = self._cache()
        E.quarantine_shared_images(cache, write=False)
        assert "image:touren|tr79|18x8" in cache


class TestLoadSpeedSuffixDoesNotSplitAProduct:
    """North Country states the load/speed pair in two places for the same
    tire, so one product arrived under two keys and bought two photos:

        'WINGUARD winSpike 3 86T XL (Studdable #12)'
        'WINGUARD winSpike 3 (Studdable #12) - 92T XL'

    On the 2026-08-30 run that split 19 products across 219 keys and bought
    154 photos of tires already photographed.
    """

    def test_both_spellings_reach_the_same_key(self):
        a = E._clean_model_text("WINGUARD winSpike 3 86T XL (Studdable #12)")
        b = E._clean_model_text("WINGUARD winSpike 3 (Studdable #12) - 92T XL")
        assert a == b == "WINGUARD winSpike 3"
        assert (E.cache_key("image", "Nexen", a, "215/60R16", "tires")
                == E.cache_key("image", "Nexen", b, "225/45R18", "tires"))

    @pytest.mark.parametrize("text,expected", [
        ("Endeavor Snow Claw 88T", "Endeavor Snow Claw"),
        ("DISCOVERER SNOW CLAW (Studdable) - 115T", "DISCOVERER SNOW CLAW"),
        ("ALTIMAX ARCTIC X 95T XL", "ALTIMAX ARCTIC X"),
    ])
    def test_the_pair_is_stripped(self, text, expected):
        assert E._clean_model_text(text) == expected

    @pytest.mark.parametrize("text", [
        "Firehawk Indy 500",      # 500 is out of load-index range
        "VOYAGER 777EV",          # 777 is out of range
        "Ecsta PS71",             # letters first
        "Pilot Sport 4S",         # one digit, not a load index
        "GEOLANDAR A/T4 G018",
        "PRIMESTAR 66",           # digits with no speed letter
    ])
    def test_a_model_that_merely_ends_in_digits_is_untouched(self, text):
        """The bound on the load index (60-150) is what keeps these intact —
        a rule that stripped any trailing digits-plus-letter would rename
        real products."""
        assert E._clean_model_text(text) == text

    def test_it_must_run_before_normalization(self):
        """Once the spaces are gone, 'winguardwinspike386txl' cannot be told
        apart from a model named 'WinSpike' rated 386. This test pins the
        ordering that makes the strip unambiguous."""
        assert E.normalize_key_token("WINGUARD winSpike 3 86T XL") == "winguardwinspike386txl"
        assert E.normalize_key_token(
            E._clean_model_text("WINGUARD winSpike 3 86T XL")) == "winguardwinspike3"


class TestGluedLoadSpeedMigration:
    """Stripping the load/speed pair changes the key, which strands every
    answer bought under the old one. Left unmigrated the strip BACKFIRES:
    each affected product misses the cache and is bought a second time.

    migrate_cache_keys() cannot fix it, because by the time a key exists the
    model is already glued and "winguardwinspike386txl" no longer says whether
    it is WinSpike 3 rated 86T or WinSpike rated 386. Only the catalogue can say.
    """

    def _setup(self, monkeypatch, tmp_path, cache, wanted):
        f = tmp_path / "serp_cache.json"
        f.write_text(json.dumps(cache), encoding="utf-8")
        monkeypatch.setattr(E, "CACHE_FILE", str(f))
        monkeypatch.setattr(E, "catalogue_image_keys", lambda: set(wanted))
        return f

    def test_the_catalogue_disambiguates_the_stem(self, monkeypatch, tmp_path):
        """86T leaves 'winspike3'; 386 would leave 'winspike'. Only one of
        those is a key the catalogue generates, so the choice is not a guess."""
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguardwinspike386txl|": {"image": "a.jpg"}},
                    {"image:nexen|winguardwinspike3|"})
        moved, ambiguous, left = E.migrate_glued_load_speed_keys(write=True)
        assert (moved, ambiguous, left) == (1, 0, 0)
        assert E.load_cache() == {"image:nexen|winguardwinspike3|": {"image": "a.jpg"}}

    def test_an_answer_already_on_the_target_is_kept(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path, {
            "image:nexen|winguardice3104t|": {"image": "old.jpg"},
            "image:nexen|winguardice3|": {"image": "live.jpg"},
        }, {"image:nexen|winguardice3|"})
        E.migrate_glued_load_speed_keys(write=True)
        after = E.load_cache()
        assert after == {"image:nexen|winguardice3|": {"image": "live.jpg"}}

    def test_a_key_the_catalogue_still_asks_for_is_left_alone(self, monkeypatch,
                                                              tmp_path):
        self._setup(monkeypatch, tmp_path,
                    {"image:firestone|firehawkindy500|": {"image": "a.jpg"}},
                    {"image:firestone|firehawkindy500|"})
        assert E.migrate_glued_load_speed_keys(write=True)[0] == 0

    def test_an_unmatchable_answer_is_reported_not_deleted(self, monkeypatch,
                                                            tmp_path):
        """A product the catalogue dropped keeps its answer — it costs nothing
        to hold and nothing re-buys it."""
        self._setup(monkeypatch, tmp_path,
                    {"image:acme|gone95t|": {"image": "a.jpg"}}, set())
        moved, _, left = E.migrate_glued_load_speed_keys(write=True)
        assert (moved, left) == (0, 1)
        assert "image:acme|gone95t|" in E.load_cache()

    def test_it_is_idempotent(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguardwinspike386txl|": {"image": "a.jpg"}},
                    {"image:nexen|winguardwinspike3|"})
        E.migrate_glued_load_speed_keys(write=True)
        assert E.migrate_glued_load_speed_keys(write=True)[0] == 0


class TestGalleryIndexRanking:
    """PriorityTire numbers the shots in a per-tire gallery and image 1 is the
    hero tread shot. Measured over 206 of its photos labelled by hand after the
    2026-08-30 run: index 1 was wrong 3 times in 123, index 22 was wrong 16
    times out of 16, and the host overall was wrong 21% of the time.
    """

    @staticmethod
    def _cand(url):
        return {"original": url, "original_width": 1200, "original_height": 1200,
                "title": "Michelin Defender", "source": "prioritytire.com"}

    HOST = "https://backend.prioritytire.com/media/catalog/product/michelin/"

    def test_deprioritized_hosts_are_matched_on_their_cdn_too(self):
        """i.ebayimg.com and external-preview.redd.it both carry the brand but
        not the domain, so a full-hostname test missed them — both were bought
        and both were rejected by the reviewer."""
        for host in ("i.ebayimg.com", "external-preview.redd.it",
                     "image.made-in-china.com"):
            assert any(d.split(".")[0] in host for d in E.DOMAIN_DEPRIORITIZE), host

    def test_a_legitimate_retailer_is_not_caught_by_that(self):
        for host in ("backend.prioritytire.com", "images.simpletire.com",
                     "cdn.shopify.com", "tirebuyer.com"):
            assert not any(d.split(".")[0] in host for d in E.DOMAIN_DEPRIORITIZE), host

    def test_the_hero_shot_outranks_a_late_gallery_image(self):
        first = self._cand(self.HOST + "michelin-defender-b-aaa-1.jpg")
        late = self._cand(self.HOST + "michelin-defender-b-aaa-22.jpg")
        ranked = E.filter_image_candidates([late, first], "Michelin", "Defender")
        assert [c["original"] for c in ranked] == [first["original"], late["original"]]

    def test_a_late_gallery_image_is_still_available_as_a_fallback(self):
        late = self._cand(self.HOST + "michelin-defender-b-aaa-22.jpg")
        assert len(E.filter_image_candidates([late], "Michelin", "Defender")) == 1

    def test_the_rule_does_not_apply_to_unmeasured_hosts(self):
        """SimpleTire's own shot names split 15/9/23% — no signal — so nothing
        should be inferred from a number in ITS urls."""
        a = {"original": "https://images.simpletire.com/line-images/18862-1.jpg",
             "original_width": 1200, "original_height": 1200,
             "title": "Michelin Defender", "source": "simpletire.com"}
        b = {"original": "https://images.simpletire.com/line-images/18862-22.jpg",
             "original_width": 1200, "original_height": 1200,
             "title": "Michelin Defender", "source": "simpletire.com"}
        assert E._gallery_score("images.simpletire.com", a["original"]) == 0
        assert E._gallery_score("images.simpletire.com", b["original"]) == 0

    def test_a_declared_sidewall_still_loses_to_a_late_gallery_image(self):
        """The pose penalty must stay dominant over the gallery bonus."""
        side = self._cand(self.HOST + "michelin-defender-sidewall-1.jpg")
        late = self._cand(self.HOST + "michelin-defender-b-aaa-22.jpg")
        ranked = E.filter_image_candidates([side, late], "Michelin", "Defender")
        assert ranked[0]["original"] == late["original"]

    @pytest.mark.parametrize("idx,expected", [(1, 2), (2, 2), (3, -4), (4, -4),
                                              (5, -4), (22, -4)])
    def test_score_matches_the_measured_table(self, idx, expected):
        """Threshold sits at 3 because index 3 measured 44% wrong and index 4
        18%, against 4-5% for indices 1-2."""
        url = self.HOST + f"michelin-defender-b-aaa-{idx}.jpg"
        assert E._gallery_score("backend.prioritytire.com", url) == expected


class TestRequeueTreadRejected:
    """After a review the cache reaches a state nothing else resolves: the key
    has an answer, so run_live skips it as paid for, but the answer is a photo
    the reviewer threw out, so every row using it is a gap. The product is
    stuck — flagged forever, never re-bought.
    """

    def _setup(self, monkeypatch, tmp_path, cache, tread_cache):
        f = tmp_path / "serp_cache.json"
        f.write_text(json.dumps(cache), encoding="utf-8")
        monkeypatch.setattr(E, "CACHE_FILE", str(f))
        # tread.load() binds TREAD_CACHE as a default argument, so patching the
        # path would not reach it — patch the loader itself.
        monkeypatch.setattr(E._TREAD, "load", lambda *a, **k: dict(tread_cache))
        self.tread_cache = tread_cache
        return f

    def test_a_rejected_answer_is_dropped(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguard|": {"image": "https://cdn/side.jpg"}},
                    {"https://cdn/side.jpg": {"tread": "not_visible", "source": "human"}})
        assert E.requeue_tread_rejected(write=True) == 1
        assert E.load_cache() == {}

    def test_an_accepted_answer_is_kept(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguard|": {"image": "https://cdn/good.jpg"}},
                    {"https://cdn/good.jpg": {"tread": "visible", "source": "human"}})
        assert E.requeue_tread_rejected(write=True) == 0
        assert "image:nexen|winguard|" in E.load_cache()

    def test_an_unreviewed_answer_is_kept(self, monkeypatch, tmp_path):
        """Unlabelled is not a rejection — dropping it would re-buy the whole
        catalogue after every scrape."""
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguard|": {"image": "https://cdn/new.jpg"}}, {})
        assert E.requeue_tread_rejected(write=True) == 0

    def test_an_unsure_label_is_not_treated_as_a_rejection(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguard|": {"image": "https://cdn/maybe.jpg"}},
                    {"https://cdn/maybe.jpg": {"tread": "unknown", "source": "human"}})
        assert E.requeue_tread_rejected(write=True) == 0

    def test_the_rejection_itself_survives(self, monkeypatch, tmp_path):
        """The photo must stay un-selectable, or the re-buy walks straight back
        to the picture that was just thrown out."""
        self._setup(monkeypatch, tmp_path,
                    {"image:nexen|winguard|": {"image": "https://cdn/side.jpg"}},
                    {"https://cdn/side.jpg": {"tread": "not_visible", "source": "human"}})
        E.requeue_tread_rejected(write=True)
        assert tread.is_rejected("https://cdn/side.jpg", self.tread_cache)

    def test_a_quarantined_entry_is_left_alone(self, monkeypatch, tmp_path):
        """quarantine_shared_images() blanks an answer precisely so it is NOT
        re-queued; this must not undo that."""
        self._setup(monkeypatch, tmp_path,
                    {"image:touren|tr79|": {"image": "", "quarantined": {"was": "x"}}},
                    {})
        assert E.requeue_tread_rejected(write=True) == 0
        assert "image:touren|tr79|" in E.load_cache()


class TestSpecNotApplicable:
    """A flotation size states overall diameter x width on a rim —
    "LT35X12.50R17" is 35in tall, 12.50 wide, on a 17in wheel. It has no
    aspect ratio; the number does not exist for this shape of size. The live
    run still came back with aspect_ratio 55 for exactly that size, read off
    some other tire named in the result text.
    """

    @pytest.mark.parametrize("size", [
        "LT35X12.50R17", "35X12.50R20", "33x12.50R17", "LT33X12.50R18",
    ])
    def test_a_flotation_size_has_no_aspect_ratio(self, size):
        assert E.spec_not_applicable(size, "aspect_ratio")

    @pytest.mark.parametrize("size", ["225/60R18", "LT265/70R17", "P205/55R16"])
    def test_a_metric_size_does_have_one(self, size):
        assert not E.spec_not_applicable(size, "aspect_ratio")

    def test_other_subfields_are_unaffected(self):
        """Rim diameter IS in a flotation size and came back correct."""
        for sub in ("rim_diameter", "load_index", "speed_rating", "load_range",
                    "section_width"):
            assert not E.spec_not_applicable("LT35X12.50R17", sub)

    def test_a_blank_size_claims_nothing(self):
        assert not E.spec_not_applicable("", "aspect_ratio")


class TestUnusableImageFormats:
    """Shopify's product-CSV import ingests JPEG, PNG, GIF and WEBP. Anything
    else never attaches and the listing ships blank with nothing in the logs
    to explain it — so unlike pose, this cannot be a scored preference. A live
    run bought "l_zeal_56-2.heic" from a Shopify-hosted storefront: a perfectly
    good photo where it lived, useless on import.
    """

    @staticmethod
    def _c(url):
        return {"original": url, "original_width": 1200, "original_height": 1200,
                "title": "Sonix L-ZEAL56", "source": "example.com"}

    @pytest.mark.parametrize("url", [
        "https://cdn/shop/files/l_zeal_56-2.heic?v=1744666071&width=1946",
        "https://cdn/tire.heif", "https://cdn/tire.avif",
        "https://cdn/tire.bmp", "https://cdn/tire.tiff", "https://cdn/tire.svg",
    ])
    def test_an_unusable_format_is_dropped(self, url):
        assert E.filter_image_candidates([self._c(url)], "Sonix", "L-ZEAL56") == []

    @pytest.mark.parametrize("url", [
        "https://cdn/tire.jpg", "https://cdn/tire.jpeg", "https://cdn/tire.png",
        "https://cdn/tire.webp", "https://cdn/tire.jpg?v=2&width=1946",
        "https://cdn/tire",
    ])
    def test_a_usable_format_survives(self, url):
        assert len(E.filter_image_candidates([self._c(url)], "Sonix", "L-ZEAL56")) == 1

    def test_a_usable_candidate_is_preferred_over_dropping_everything(self):
        heic = self._c("https://cdn/tire.heic")
        jpg = self._c("https://cdn/tire.jpg")
        out = E.filter_image_candidates([heic, jpg], "Sonix", "L-ZEAL56")
        assert [c["original"] for c in out] == [jpg["original"]]

    def test_the_word_heic_inside_a_path_is_not_a_format(self):
        """Only a real extension counts, not the letters appearing somewhere."""
        u = "https://cdn/heic-brand/tire.jpg"
        assert len(E.filter_image_candidates([self._c(u)], "Sonix", "L-ZEAL56")) == 1
