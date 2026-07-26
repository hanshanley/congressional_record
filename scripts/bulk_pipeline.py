import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from analysis.ingest.govinfo_bulk import run_bulk
pkgs = [l.strip() for l in open("data/bulk/_pkglist_full.txt") if l.strip()]
n = run_bulk(pkgs, Path("data/bulk"), Path("data/interim"), workers=12)
print("BULK_PIPELINE_DONE turns:", n)
