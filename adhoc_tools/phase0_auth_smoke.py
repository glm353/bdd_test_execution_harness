"""Phase 0 - auth smoke test (siloed live-AWS validation, ad hoc).

Confirms the Okta-SSO profile resolves and creds are valid (STS preflight), then reports the Athena
workgroup the component would auto-discover. No Athena queries, no Glue crawl - the cheapest possible
live check before we scale up. NOT part of Component 1.

Run: python adhoc_tools/phase0_auth_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import util


def main() -> int:
    cfg = util.load_config()
    print(f"profile = {cfg.profile!r}   region = {cfg.region!r}   env = {cfg.env_code!r}")

    # 1) STS preflight - proves the creds are live.
    session = util.resolve_session(profile=cfg.profile, region=cfg.region)
    ident = session.client("sts").get_caller_identity()
    print(f"[sts] Account = {ident['Account']}")
    print(f"[sts] Arn     = {ident['Arn']}")

    # 2) Athena workgroup the component would pick (auto-discovery: first with an OutputLocation).
    src = util.AwsWatermarkSource(cfg, session=session)
    wg = src._discover_workgroup()
    print(f"[athena] resolved workgroup = {wg!r}")

    print("\nPhase 0 OK - creds valid, workgroup resolved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
