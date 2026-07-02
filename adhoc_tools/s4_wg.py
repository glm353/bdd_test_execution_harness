"""Session 4 throwaway: discover the dev3 workgroup's S3 output location (for a scratch Iceberg LOCATION)."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

src = util.AwsWatermarkSource(util.load_config("dev"))
ath = src._client("athena")
wg = ath.get_work_group(WorkGroup="dev3")["WorkGroup"]
cfg = wg.get("Configuration", {})
out = cfg.get("ResultConfiguration", {}).get("OutputLocation")
print("dev3 OutputLocation =", out)
