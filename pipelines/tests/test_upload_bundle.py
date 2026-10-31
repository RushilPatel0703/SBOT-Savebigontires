"""Regression tests for the Handle-uniqueness safety net in upload_bundle.py.

Real-world trigger: an empty Shopify store rejected 24 rows across the 20 Aug
2026 bundle with "ignored because handle X already exists" -- on an empty
store, that error can only mean two rows in the SAME import batch collided on
Handle. Investigation found 19 such pairs, all with distinct SKUs (often
distinct prices too), so none were true duplicate rows -- each pair described
two separately-purchasable products that happened to slugify identically:
a "(Y)" vs "Y" speed-rating notation, a "P-ZERO" vs "P ZERO" spacing
difference in the model name, and Nexen SKU variants with no textual
difference in the displayed fields at all. Three unrelated root causes, so
the fix isn't a slugify() patch -- it's a uniqueness pass over the merged
bundle that can't miss a case regardless of why the text collided.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import upload_bundle as UB  # noqa: E402


def _df(rows):
    return pd.DataFrame(rows)


def test_no_collisions_leaves_handles_untouched():
    df = _df([
        {"Handle": "a", "Variant SKU": "111"},
        {"Handle": "b", "Variant SKU": "222"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 0
    assert list(out["Handle"]) == ["a", "b"]


def test_first_occurrence_keeps_original_handle():
    df = _df([
        {"Handle": "same", "Variant SKU": "AAA111"},
        {"Handle": "same", "Variant SKU": "BBB222"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[0, "Handle"] == "same"
    assert out.loc[1, "Handle"] != "same"


def test_disambiguated_handle_is_traceable_to_sku():
    df = _df([
        {"Handle": "bridgestone-potenza-24535r20-95y-xl", "Variant SKU": "013731"},
        {"Handle": "bridgestone-potenza-24535r20-95y-xl", "Variant SKU": "013367"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[1, "Handle"] == "bridgestone-potenza-24535r20-95y-xl-013367"


def test_three_way_collision_all_become_unique():
    df = _df([
        {"Handle": "x", "Variant SKU": "111"},
        {"Handle": "x", "Variant SKU": "222"},
        {"Handle": "x", "Variant SKU": "333"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 2
    assert out["Handle"].nunique() == 3


def test_result_has_zero_duplicate_handles_regardless_of_group_count():
    df = _df([
        {"Handle": "a", "Variant SKU": "1"},
        {"Handle": "a", "Variant SKU": "2"},
        {"Handle": "b", "Variant SKU": "3"},
        {"Handle": "b", "Variant SKU": "4"},
        {"Handle": "b", "Variant SKU": "5"},
        {"Handle": "c", "Variant SKU": "6"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 3
    assert not out["Handle"].duplicated().any()


def test_falls_back_to_dup_suffix_when_sku_column_missing():
    df = _df([
        {"Handle": "same"},
        {"Handle": "same"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[1, "Handle"] == "same-dup"


def test_falls_back_to_dup_suffix_when_sku_is_blank():
    df = _df([
        {"Handle": "same", "Variant SKU": "111"},
        {"Handle": "same", "Variant SKU": ""},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[1, "Handle"] == "same-dup"


def test_suffix_collision_falls_back_to_numeric_disambiguator():
    # Two different SKUs that happen to produce the identical cleaned suffix.
    df = _df([
        {"Handle": "same", "Variant SKU": "ABC-123"},
        {"Handle": "same", "Variant SKU": "abc123"},
        {"Handle": "same", "Variant SKU": "  abc123  "},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 2
    assert out["Handle"].nunique() == 3


def test_no_handle_column_is_a_noop():
    df = _df([{"Variant SKU": "111"}, {"Variant SKU": "222"}])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 0
    assert list(out.columns) == ["Variant SKU"]


def test_row_count_and_non_handle_data_are_unchanged():
    df = _df([
        {"Handle": "same", "Variant SKU": "111", "Variant Price": "9.99"},
        {"Handle": "same", "Variant SKU": "222", "Variant Price": "19.99"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert len(out) == 2
    assert list(out["Variant Price"]) == ["9.99", "19.99"]
    assert list(out["Variant SKU"]) == ["111", "222"]


# ── freshness: a bundle is newer than everything in it and can still be stale ──

class TestStaleSourceGuard:
    """A backfill rewrites a supplier's cleaned CSV; the Shopify CSV is only
    regenerated if something re-runs that supplier's augment step, and nothing
    forces it to.

    On 2026-08-21 the DT Tire wheels backfill fixed 505 of 514 images in
    `cleaned_wheels_*.csv` and the bundle still shipped 130px thumbnails,
    because `Shopify_SEO_dttire_wheels_*` had been written the day before.
    The folder timestamp cannot catch that — the bundle genuinely IS newer
    than every file in it. Only comparing each output against its own input
    can.
    """

    def _tree(self, tmp_path, out_age, src_age):
        """A one-supplier tree: a Shopify CSV and the cleaned CSV behind it."""
        import os
        sup = tmp_path / "dttire"
        (sup / "Data" / "shopify").mkdir(parents=True)
        (sup / "Data" / "cleaned").mkdir(parents=True)
        out = sup / "Data" / "shopify" / "Shopify_SEO_dttire_wheels_20260101_000000.csv"
        src = sup / "Data" / "cleaned" / "cleaned_wheels_20260101_000000.csv"
        out.write_text("Handle,Image Src\na,x.jpg\n", encoding="utf-8")
        src.write_text("sku,image_url\n1,x.jpg\n", encoding="utf-8")
        os.utime(out, (out_age, out_age))
        os.utime(src, (src_age, src_age))
        return out, src

    def _patch_sources(self, monkeypatch, src):
        import enrichment as E
        monkeypatch.setattr(E, "SOURCES", [dict(
            supplier="dttire", type="wheels", field="image", glob=str(src),
            brand_col="brand", model_col="description", size_col="size",
            extra_col=None, desc_col="description", value_col="image_url",
            writable=True)])

    def test_an_output_older_than_its_input_is_flagged(self, tmp_path, monkeypatch):
        out, src = self._tree(tmp_path, out_age=1_000_000, src_age=2_000_000)
        self._patch_sources(monkeypatch, src)
        stale = UB.stale_sources([out])
        assert [(o.name, i.name) for o, i in stale] == [(out.name, src.name)]

    def test_an_output_newer_than_its_input_is_not_flagged(self, tmp_path, monkeypatch):
        out, src = self._tree(tmp_path, out_age=2_000_000, src_age=1_000_000)
        self._patch_sources(monkeypatch, src)
        assert UB.stale_sources([out]) == []

    def test_building_refuses_on_a_stale_source(self, tmp_path, monkeypatch):
        out, src = self._tree(tmp_path, out_age=1_000_000, src_age=2_000_000)
        self._patch_sources(monkeypatch, src)
        files, error = UB.prepare_upload_files(
            [tmp_path / "dttire"], out_root=tmp_path / "out")
        assert files == []
        assert "out of date" in error and out.name in error
        assert not (tmp_path / "out").exists(), "nothing may be written on refusal"

    def test_allow_stale_builds_anyway(self, tmp_path, monkeypatch, capsys):
        out, src = self._tree(tmp_path, out_age=1_000_000, src_age=2_000_000)
        self._patch_sources(monkeypatch, src)
        files, error = UB.prepare_upload_files(
            [tmp_path / "dttire"], out_root=tmp_path / "out", allow_stale=True)
        assert error == "" and files
        assert "--allow-stale" in capsys.readouterr().out

    def test_an_unmappable_output_never_blocks_a_build(self, tmp_path, monkeypatch):
        """An output whose name carries no tires/wheels/rims token has no input
        to compare against. Unknown is not the same as stale."""
        out, src = self._tree(tmp_path, out_age=1_000_000, src_age=2_000_000)
        odd = out.parent / "Shopify_SEO_dttire_something_20260101_000000.csv"
        out.rename(odd)
        self._patch_sources(monkeypatch, src)
        assert UB.stale_sources([odd]) == []

    def test_a_broken_enrichment_import_does_not_block_a_build(self, tmp_path, monkeypatch):
        """The freshness check is best-effort. It must never be the reason a
        bundle cannot be produced."""
        out, _src = self._tree(tmp_path, out_age=1_000_000, src_age=2_000_000)
        import builtins
        real = builtins.__import__

        def boom(name, *a, **kw):
            if name == "enrichment":
                raise ImportError("simulated")
            return real(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", boom)
        monkeypatch.delitem(sys.modules, "enrichment", raising=False)
        assert UB.stale_sources([out]) == []


class TestNewProductsOnlyFilter:
    """Bundling only products the store does not have yet.

    An import exists to CREATE products. Re-uploading the ~15.6k rows that
    already exist changes nothing (price and quantity are the sync's job) and
    turns one 4 MB file into several 15 MB ones. The risk in filtering is
    getting "already exists" wrong in either direction: dropping a genuinely new
    product means it never reaches the store, and keeping everything means the
    filter silently did nothing.
    """

    def test_keeps_only_skus_absent_from_the_store(self):
        df = _df([
            {"Handle": "a", "Variant SKU": "IN-STORE"},
            {"Handle": "b", "Variant SKU": "NEW-1"},
            {"Handle": "c", "Variant SKU": "NEW-2"},
        ])
        kept, dropped = UB.filter_new_products(df, {"IN-STORE"})
        assert dropped == 1
        assert list(kept["Variant SKU"]) == ["NEW-1", "NEW-2"]

    def test_leading_zeros_are_significant(self):
        """Both sides of the comparison are the SKU as Shopify holds it, so
        '000539' and '539' are different products. Normalising either side
        would invent a match the store does not have."""
        df = _df([
            {"Handle": "a", "Variant SKU": "000539"},
            {"Handle": "b", "Variant SKU": "539"},
        ])
        kept, dropped = UB.filter_new_products(df, {"539"})
        assert dropped == 1
        assert list(kept["Variant SKU"]) == ["000539"]

    def test_surrounding_whitespace_still_matches(self):
        df = _df([{"Handle": "a", "Variant SKU": " ABC "}])
        kept, dropped = UB.filter_new_products(df, {"ABC"})
        assert dropped == 1
        assert kept.empty

    def test_empty_store_set_keeps_everything(self):
        df = _df([{"Handle": "a", "Variant SKU": "X"},
                  {"Handle": "b", "Variant SKU": "Y"}])
        kept, dropped = UB.filter_new_products(df, set())
        assert dropped == 0
        assert len(kept) == 2

    def test_missing_sku_column_is_left_alone(self):
        """filter_new_products is a pure helper; prepare_upload_files is what
        refuses the build, so this must not silently drop every row."""
        df = _df([{"Handle": "a"}])
        kept, dropped = UB.filter_new_products(df, {"X"})
        assert dropped == 0
        assert len(kept) == 1


class TestNewProductsOnlyBuild:
    """The two ways a filtered build can be wrong enough to refuse."""

    @staticmethod
    def _supplier_dir(tmp_path, skus):
        d = tmp_path / "supplier" / "Data" / "shopify"
        d.mkdir(parents=True)
        pd.DataFrame([
            {"Handle": f"h-{s}", "Variant SKU": s, "Title": f"T {s}"} for s in skus
        ]).to_csv(d / "Shopify_SEO_tires_20260101_000000.csv", index=False)
        return tmp_path / "supplier"

    def test_refuses_when_nothing_is_new(self, tmp_path, monkeypatch):
        monkeypatch.setattr(UB, "stale_sources", lambda s: [])
        src = self._supplier_dir(tmp_path, ["A", "B"])
        files, err = UB.prepare_upload_files(
            [src], out_root=tmp_path / "out", exclude_skus={"A", "B"})
        assert files == []
        assert "already exists in the store" in err

    def test_refuses_when_the_filter_matched_nothing(self, tmp_path, monkeypatch):
        """A non-empty store that overlaps the CSVs in zero places means the two
        sides are not talking about the same SKUs — a stale mirror, or SKU
        formats that have diverged. Bundling the whole catalogue under a "new
        products" label is worse than stopping."""
        monkeypatch.setattr(UB, "stale_sources", lambda s: [])
        src = self._supplier_dir(tmp_path, ["A", "B"])
        files, err = UB.prepare_upload_files(
            [src], out_root=tmp_path / "out", exclude_skus={"totally", "unrelated"})
        assert files == []
        assert "not credible" in err

    def test_writes_a_distinctly_named_bundle(self, tmp_path, monkeypatch):
        """A new-products bundle and a full one are imported identically, so the
        name is the only thing keeping them apart in upload_ready/."""
        monkeypatch.setattr(UB, "stale_sources", lambda s: [])
        src = self._supplier_dir(tmp_path, ["A", "B", "C"])
        files, err = UB.prepare_upload_files(
            [src], out_root=tmp_path / "out", exclude_skus={"A"})
        assert err == ""
        assert len(files) == 1
        assert files[0].name == "shopify_new_products.csv"
        assert "(new products)" in files[0].parent.name

        written = pd.read_csv(files[0], dtype=str, keep_default_na=False)
        assert sorted(written["Variant SKU"]) == ["B", "C"]

        manifest = (files[0].parent / "manifest.txt").read_text(encoding="utf-8")
        assert "Contents: NEW products only" in manifest
        assert "Rows skipped as already in the store: 1" in manifest

    def test_unfiltered_build_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(UB, "stale_sources", lambda s: [])
        src = self._supplier_dir(tmp_path, ["A", "B"])
        files, err = UB.prepare_upload_files([src], out_root=tmp_path / "out")
        assert err == ""
        assert files[0].name == "shopify_upload.csv"
        assert "(new products)" not in files[0].parent.name
        assert len(pd.read_csv(files[0], dtype=str)) == 2


# ── Supplier tag ──────────────────────────────────────────────────────────────
class TestSupplierTag:
    """Every product in a bundle carries the tag naming its supplier. The client
    filters the store by it, so a row without one cannot be traced back."""

    @staticmethod
    def _csv(tmp_path, supplier, rows):
        d = tmp_path / supplier / "Data" / "shopify"
        d.mkdir(parents=True)
        path = d / "Shopify_SEO_wheels_20260101_000000.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return path

    def test_missing_tag_is_added_from_the_supplier_directory(self, tmp_path):
        path = self._csv(tmp_path, "dttire", [
            {"Title": "A", "Tags": "Brand_X,WheelDia_17"},
            {"Title": "B", "Tags": ""},
        ])
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        out, added = UB.ensure_supplier_tag(frame, path)
        assert added == 2
        assert list(out["Tags"]) == ["Brand_X,WheelDia_17,Supplier_Dttire", "Supplier_Dttire"]

    def test_existing_supplier_tag_is_left_alone(self, tmp_path):
        path = self._csv(tmp_path, "northcountry", [
            {"Title": "A", "Tags": "Brand_X,Supplier_Northcountry"},
        ])
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        out, added = UB.ensure_supplier_tag(frame, path)
        assert added == 0
        assert out.loc[0, "Tags"] == "Brand_X,Supplier_Northcountry"

    def test_image_only_rows_are_not_tagged(self, tmp_path):
        path = self._csv(tmp_path, "wheelsmart", [
            {"Title": "A", "Tags": "Brand_X"},
            {"Title": "",  "Tags": ""},
        ])
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        out, added = UB.ensure_supplier_tag(frame, path)
        assert added == 1
        assert out.loc[1, "Tags"] == ""

    def test_bundle_rows_all_carry_a_supplier_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(UB, "stale_sources", lambda s: [])
        self._csv(tmp_path, "emergetires", [
            {"Handle": "a", "Variant SKU": "EMRG-1", "Title": "A", "Tags": "Brand_X"},
        ])
        self._csv(tmp_path, "formula_distributors", [
            {"Handle": "b", "Variant SKU": "2", "Title": "B", "Tags": "Supplier_Formulatires"},
        ])
        files, err = UB.prepare_upload_files(
            [tmp_path / "emergetires", tmp_path / "formula_distributors"],
            out_root=tmp_path / "out")
        assert err == ""
        written = pd.read_csv(files[0], dtype=str, keep_default_na=False)
        assert dict(zip(written["Variant SKU"], written["Tags"])) == {
            "EMRG-1": "Brand_X,Supplier_Emergetires",
            "2": "Supplier_Formulatires",
        }

    def test_augment_steps_write_the_same_tags(self):
        """The augment steps write the tag first; this module only backfills.
        Both must agree, or one supplier's products split across two tags."""
        pipelines = Path(UB.__file__).resolve().parent.parent
        for supplier, tag in UB.SUPPLIER_TAGS.items():
            sources = [p.read_text(encoding="utf-8")
                       for p in (pipelines / supplier).glob("augment*.py")]
            assert sources, supplier
            assert all(tag in s for s in sources), f"{supplier}: augment does not write {tag}"
