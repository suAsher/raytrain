"""End-to-end smoke: pack the raytrain repo itself into a code zip.

Run from the raytrain repo root:
    python3 scripts/e2e_code_sync_smoke.py
"""
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))

from raytrain.code_sync import build_code_zip

bundle = build_code_zip(
    workdir=repo,
    job_name="smoke-test-001",
    user="dev",
    out_dir=str(repo / ".raytrain-cache"),
)

print(f"OK: zip = {bundle.zip_path}")
print(f"    size  = {bundle.size_mib:.2f} MiB")
print(f"    files = {bundle.file_count}")
print(f"    sha256= {bundle.sha256[:16]}...")
print()
print("top largest files in the bundle:")
for p, sz in bundle.excluded_top_files[:8]:
    print(f"  {sz / 1024:7.1f} KiB  {p}")
