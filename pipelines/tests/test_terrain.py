import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
import pytest
from terrain import detect_terrain, terrain_tag


@pytest.mark.parametrize("text,want", [
    ("Trail Blade A/T - 109S 6PR 3PMS", "All-Terrain"),
    ("WILDPEAK A/T4W - 6PR 3PMS", "All-Terrain"),
    ("Grabber A/TX RWL", "All-Terrain"),
    ("AT4W", "All-Terrain"),
    ("Wrangler AT Adventure", "All-Terrain"),
    ("OPEN COUNTRY A/TIII  - All terrain", "All-Terrain"),
    ("Open Country M/T - 10PR", "Mud-Terrain"),
    ("MUD TRAC - 6PR", "Mud-Terrain"),
    ("Wildpeak M/T MT01", "Mud-Terrain"),
    ("CATCHFORS A/S", ""),
    ("Hill Tracker H/T", ""),
    ("TerrainContact H/T", ""),
    ("Proxes Sport", ""),
    ("WINTERGRIP", ""),
    ("Eagle F1 Asym SUV A/T", ""),
    ("", ""),
    (None, ""),
])
def test_detect_terrain(text, want):
    assert detect_terrain(text) == want


def test_a_snowflake_a_t_tire_keeps_both_facts():
    assert terrain_tag("TBAT", "Trail Blade A/T - 109S 6PR 3PMS") == "Terrain_All-Terrain"
