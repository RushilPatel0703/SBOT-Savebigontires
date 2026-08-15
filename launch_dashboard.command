#!/usr/bin/env bash
# Save Big On Tires — Inventory Dashboard (macOS)
# Double-click this file in Finder to launch.

set -e
cd "$(dirname "$0")"

# Activate virtual environment if present
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

cd pipelines

echo ""
echo " ============================================"
echo "  Save Big On Tires — Inventory Dashboard"
echo " ============================================"
echo ""
echo " Opening dashboard in your browser..."
echo " URL: http://localhost:8501"
echo ""
echo " IMPORTANT: Keep this window open while"
echo " pipelines are running."
echo ""

python3 -m streamlit run dashboard.py --server.headless false
