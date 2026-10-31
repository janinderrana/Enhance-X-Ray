#!/bin/bash
# usage: run_e2e.sh <with|without> <title> <asc dir> <text json> <asin>
cd "$(dirname "$0")/.."
FLAG=$([ "$1" = with ] && echo yes || echo no)
/usr/bin/caffeinate -i ~/.claude-xray/venv/bin/python -I -c "import sys; sys.path.insert(0,'.'); import enrich; sys.argv=['enrich.py']+sys.argv[1:]; sys.exit(enrich.main())" \
  --xray "$3/XRAY.entities.$5.asc" --text "$4" --provider local --merge no \
  --model-high Qwen3.6-35B-A3B-oQ4e-mtp --model-bulk Qwen3.6-35B-A3B-oQ4e-mtp --page-aware yes --title "$2" \
  --wiki-check yes --cast-pass $FLAG --checkpoint "$3/layers.json" > "$3/run.log" 2>&1
echo "exit $?" >> "$3/run.log"
