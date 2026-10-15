"""Builds registry/cadastral_manifest.json, the authenticated cadastral manifest
that AetherClaim validators check every plot against (verify_plot_provenance).

This file restates the registry independently of contracts/aether_claim.py, and
computes the cadastral commitment with its own SHA-256 code. If the two ever
drift, verify_plot_provenance returns MISMATCH_* and the plot stays uninsurable.

    python3 registry/build_manifest.py            # rewrite the manifest
    python3 registry/build_manifest.py --check    # exit 1 if the file is stale

The eligible policyholder is the farm operator for the plot. Pass it with
--operator (defaults to the deployment's farmer account).
"""

import hashlib
import json
import sys
from pathlib import Path

SCHEMA = "AETHERCLAIM-CADASTRE-V1"
MAX_PLOT_COVERAGE = 50 * 10**18
DEFAULT_OPERATOR = "0xbe43375329eb4a3cafc344f0e61859905aba2b36"

# plot_id: (name, lat_e4, lon_e4, monthly mm Jan..Dec, crop start, crop end, GHCN-D station)
PLOTS = {
    "CV-ALMOND-01": ("Central Valley Almond Basin", 366000, -1198000,
                     [50, 45, 35, 15, 8, 2, 0, 0, 4, 15, 25, 45], 11, 3, "USW00093193"),
    "IA-CORN-01": ("Iowa Corn Belt", 420300, -938200,
                   [25, 28, 55, 95, 120, 140, 115, 110, 85, 70, 50, 35], 4, 10, "USC00130200"),
    "AND-OLIVE-01": ("Andalusia Olive Groves", 373800, -43700,
                     [60, 55, 50, 45, 30, 10, 2, 3, 25, 60, 75, 70], 10, 3, "SP000008410"),
}


def digest(plot_id, lat, lon, monthly, c_start, c_end, max_cov, station):
    rec = "|".join([SCHEMA, plot_id, str(lat), str(lon), ",".join(map(str, monthly)),
                    str(c_start), str(c_end), str(max_cov), station])
    return hashlib.sha256(rec.encode("utf-8")).hexdigest()


def build(operator):
    plots = {}
    for pid, (name, lat, lon, monthly, cs, ce, station) in PLOTS.items():
        plots[pid] = {
            "name": name,
            "cadastral_hash": digest(pid, lat, lon, monthly, cs, ce, MAX_PLOT_COVERAGE, station),
            "latitude_e4": lat,
            "longitude_e4": lon,
            "monthly_baselines": monthly,
            "crop_cycle_start_month": cs,
            "crop_cycle_end_month": ce,
            "max_plot_coverage": str(MAX_PLOT_COVERAGE),
            "ghcnd_station": station,
            "eligible_policyholder": operator.lower(),
        }
    return {"schema": SCHEMA, "plots": plots}


def main(argv):
    operator = DEFAULT_OPERATOR
    if "--operator" in argv:
        operator = argv[argv.index("--operator") + 1]
    out = Path(__file__).with_name("cadastral_manifest.json")
    text = json.dumps(build(operator), indent=2) + "\n"
    if "--check" in argv:
        return 0 if out.exists() and out.read_text() == text else 1
    out.write_text(text)
    print(f"wrote {out}  sha256={hashlib.sha256(text.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
