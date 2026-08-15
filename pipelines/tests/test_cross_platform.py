"""
Cross-platform path and file handling tests.

Covers:
- Path() construction consistency across OS separators
- Glob pattern behaviour (case sensitivity)
- st_mtime sort correctness when folder names would sort differently alphabetically
  (e.g. "9 July 2026" vs "10 July 2026" — '9' > '1' lexicographically)
- os.path.join vs pathlib / operator consistency
- File encoding round-trip (UTF-8 with BOM, Windows-1252 fallback)
"""

import os
import platform
import sys
import time
from pathlib import Path

import pytest


# ─── Path construction ────────────────────────────────────────────────────────

class TestPathConstruction:
    def test_path_slash_operator_matches_os_path_join(self, tmp_path):
        """pathlib '/' operator and os.path.join must produce the same absolute path."""
        a = tmp_path / "foo" / "bar" / "baz.csv"
        b = Path(os.path.join(str(tmp_path), "foo", "bar", "baz.csv"))
        assert a == b

    def test_path_str_uses_os_separator(self, tmp_path):
        """str(Path(...)) should use the OS native separator."""
        p = tmp_path / "sub" / "file.txt"
        sep = os.sep
        # On Windows sep is '\', on POSIX it is '/'
        assert sep in str(p)

    def test_path_resolve_is_absolute(self, tmp_path):
        p = (tmp_path / "mysubdir_a" / ".." / "b").resolve()
        assert p.is_absolute()
        # resolve() should eliminate the ".." so "mysubdir_a" disappears from the path
        assert "mysubdir_a" not in str(p)
        # and the final component must be "b"
        assert p.name == "b"

    def test_relative_path_construction_consistent(self, tmp_path):
        """
        Scripts use relative paths internally (e.g. 'Data/file.csv').
        Joining them to an absolute base must work the same on all platforms.
        """
        base = tmp_path
        rel  = Path("Data") / "file.csv"
        full = base / rel
        assert full == base / "Data" / "file.csv"

    def test_path_parent_consistent(self, tmp_path):
        p = tmp_path / "a" / "b" / "c.py"
        assert p.parent == tmp_path / "a" / "b"
        assert p.parent.parent == tmp_path / "a"

    def test_path_suffix_and_stem(self):
        p = Path("Shopify_SEO_tires_20260709_083000.csv")
        assert p.suffix == ".csv"
        assert p.stem == "Shopify_SEO_tires_20260709_083000"

    def test_path_with_spaces(self, tmp_path):
        """Folder names with spaces (as created by prepare_upload_files) must work."""
        folder = tmp_path / "9 July 2026 14-30-22"
        folder.mkdir()
        f = folder / "shopify_upload.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        assert f.exists()
        assert f.read_text(encoding="utf-8").startswith("a,b")


# ─── Glob case sensitivity ────────────────────────────────────────────────────

class TestGlobCaseSensitivity:
    """
    macOS HFS+ is case-insensitive; Linux ext4 is case-sensitive.
    Windows NTFS is case-insensitive.
    Tests document the actual behaviour — a test will fail on Linux if the
    production code relies on case-insensitive glob matching.
    """

    def test_exact_case_glob_always_works(self, tmp_path):
        """Exact-case glob must work on every platform."""
        d = tmp_path / "Data"
        d.mkdir()
        (d / "Shopify_SEO_tires_20260709_083000.csv").write_text("h\n", encoding="utf-8")
        matches = list(d.glob("Shopify_SEO_*.csv"))
        assert len(matches) == 1

    def test_lowercase_glob_platform_behaviour(self, tmp_path):
        """
        Document case-sensitivity: 'shopify_*.csv' vs file named 'Shopify_*.csv'.
        On Linux this will return 0 matches (case-sensitive).
        On Windows/macOS this will return 1 match (case-insensitive FS).
        The test records the platform behaviour as a known fact; it does NOT assert
        that the dashboard handles both — that would require a code change.
        """
        d = tmp_path / "Data"
        d.mkdir()
        (d / "Shopify_SEO_tires_20260709_083000.csv").write_text("h\n", encoding="utf-8")
        matches = list(d.glob("shopify_SEO_*.csv"))  # wrong case on capital S
        if sys.platform in ("win32", "darwin"):
            # Case-insensitive FS: glob finds it anyway
            assert len(matches) == 1, (
                "Expected case-insensitive match on Windows/macOS"
            )
        else:
            # Linux: case-sensitive FS — expected to return nothing
            assert len(matches) == 0, (
                "Expected case-sensitive miss on Linux — "
                "production code must use exact case 'Shopify_SEO_*.csv'"
            )

    def test_production_glob_pattern_is_exact_case(self, tmp_path):
        """
        Verify that the glob pattern used in collect_source_csvs ("Shopify_SEO_*.csv")
        matches exactly when the file is capitalised correctly.
        This is the only safe cross-platform pattern.
        """
        d = tmp_path / "Data" / "shopify"
        d.mkdir(parents=True)
        fname = "Shopify_SEO_tires_20260709_083000.csv"
        (d / fname).write_text("h\n", encoding="utf-8")
        matches = list(d.glob("Shopify_SEO_*.csv"))
        assert any(m.name == fname for m in matches)


# ─── mtime sort (folder names that diverge from alphabetical order) ───────────

class TestMtimeSort:
    """
    dashboard.py uses st_mtime to sort upload_ready/ folders, with a comment
    noting that "9 July" sorts after "10 July" alphabetically ('9' > '1').
    These tests confirm that mtime sort produces the correct chronological order.
    """

    def _make_folder(self, parent: Path, name: str, mtime_offset: float) -> Path:
        p = parent / name
        p.mkdir(parents=True, exist_ok=True)
        f = p / "dummy.csv"
        f.write_text("x", encoding="utf-8")
        t = 1_700_000_000.0 + mtime_offset
        os.utime(p, (t, t))
        return p

    def test_mtime_sort_beats_alpha_sort(self, tmp_path):
        """
        '10 July 2026' was created AFTER '9 July 2026'.
        Alphabetically '9' > '1' so naïve sort puts '9 July' first.
        mtime sort must put '10 July' first (it is newer).
        """
        d = tmp_path / "upload_ready"
        d.mkdir()
        folder_9  = self._make_folder(d, "9 July 2026 10-00-00",  mtime_offset=0)
        folder_10 = self._make_folder(d, "10 July 2026 09-00-00", mtime_offset=3600)

        # Alpha sort (wrong)
        alpha = sorted(d.glob("*/"), key=lambda p: p.name, reverse=True)
        assert alpha[0].name.startswith("9 "), "Confirm alpha puts '9 July' first"

        # mtime sort (correct — as used in dashboard.py)
        by_mtime = sorted(d.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True)
        assert by_mtime[0].name.startswith("10 "), (
            "mtime sort should put '10 July' first (it is newer)"
        )

    def test_mtime_sort_three_folders(self, tmp_path):
        """Three folders created in chronological order → mtime sort matches creation order."""
        d = tmp_path / "upload_ready"
        d.mkdir()
        self._make_folder(d, "1 July 2026 08-00-00",  mtime_offset=0)
        self._make_folder(d, "9 July 2026 08-00-00",  mtime_offset=100)
        self._make_folder(d, "10 July 2026 08-00-00", mtime_offset=200)

        by_mtime = sorted(d.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True)
        names = [p.name for p in by_mtime]
        assert names[0].startswith("10 "), f"Newest should be first, got: {names}"
        assert names[2].startswith("1 July"),  f"Oldest should be last, got: {names}"

    def test_stat_mtime_is_float(self, tmp_path):
        """st_mtime returns a float on all platforms (seconds since epoch)."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        mtime = f.stat().st_mtime
        assert isinstance(mtime, float)
        assert mtime > 0

    def test_utime_sets_mtime(self, tmp_path):
        """os.utime() can control mtime — confirms our test infrastructure is correct."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        target = 1_600_000_000.0
        os.utime(f, (target, target))
        assert abs(f.stat().st_mtime - target) < 2  # within 2 seconds (FAT32 resolution)


# ─── os.path.join vs Path / operator ─────────────────────────────────────────

class TestPathOperatorVsOsPathJoin:
    def test_absolute_base_both_methods_agree(self, tmp_path):
        base_str  = str(tmp_path)
        via_join  = Path(os.path.join(base_str, "a", "b.csv"))
        via_slash = tmp_path / "a" / "b.csv"
        assert via_join == via_slash

    def test_mixed_separator_normpath(self):
        """os.path.normpath cleans mixed separators on Windows."""
        raw = "pipelines\\formula_distributors/Data/shopify"
        normalized = os.path.normpath(raw)
        # On Windows, this becomes 'pipelines\formula_distributors\Data\shopify'
        # On POSIX,   this becomes 'pipelines\\formula_distributors/Data/shopify' (unchanged)
        assert isinstance(normalized, str)
        # Must not contain double slashes
        assert "//" not in normalized
        assert "\\\\" not in normalized

    def test_path_from_string_with_forward_slash(self):
        """Path() on Windows accepts forward slashes and normalises them."""
        p = Path("pipelines/formula_distributors/Data")
        parts = p.parts
        # On all platforms, parts should NOT contain '/' as a literal component
        assert all("/" not in part for part in parts)


# ─── Encoding round-trips ─────────────────────────────────────────────────────

class TestFileEncoding:
    def test_utf8_bom_is_readable(self, tmp_path):
        """
        Some Windows tools write UTF-8 BOM (EF BB BF). The dashboard reads logs
        with errors='replace', but status JSON uses read_text(encoding='utf-8').
        BOM should not corrupt JSON parsing.
        """
        p = tmp_path / "bom.json"
        # Write with BOM
        p.write_bytes(b"\xef\xbb\xbf" + b'{"key": "value"}')
        try:
            import json
            # utf-8-sig strips the BOM; plain utf-8 may keep it
            text_sig = p.read_text(encoding="utf-8-sig")
            assert json.loads(text_sig) == {"key": "value"}
        except Exception as e:
            pytest.fail(f"UTF-8 BOM file read failed: {e}")

    def test_log_with_replacement_chars_no_crash(self, tmp_path):
        """
        Logs read with errors='replace' should not raise even if they contain
        bytes that are invalid UTF-8 (common when a scraper emits Windows-1252).
        """
        p = tmp_path / "mixed.log"
        # Write some valid UTF-8 + some invalid bytes (Windows-1252 em-dash 0x96)
        p.write_bytes(
            b"2026-07-10 [INFO] Pipeline start \x96 3 step(s): ['a','b','c']\n"
            b"2026-07-10 [INFO] Step 'a' \x96 OK\n"
        )
        content = p.read_text(encoding="utf-8", errors="replace")
        assert "Pipeline start" in content
        assert "�" in content  # replacement character present

    def test_pythonutf8_env_var_propagated(self):
        """
        dashboard.py sets PYTHONUTF8=1 in subprocess env. Verify that this env var
        name is a string (not bytes) and valid for os.environ assignment.
        """
        env = {**os.environ, "PYTHONUTF8": "1"}
        assert env["PYTHONUTF8"] == "1"
        # Confirm it doesn't break os.environ-style access
        assert isinstance(env.get("PYTHONUTF8"), str)

    def test_json_status_file_encoding(self, tmp_path):
        """Status file must survive a unicode string (emoji) save/load on all platforms."""
        import json
        p = tmp_path / "status.json"
        data = {"label": "❄️ Winter ☀️ All-Season"}
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        loaded = json.loads(p.read_text(encoding="utf-8"))
        assert loaded == data
