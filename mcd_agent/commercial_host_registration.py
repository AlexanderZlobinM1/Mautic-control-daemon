"""MCD-owned host-first Portal registration entrypoint."""

from __future__ import annotations

import argparse
import json

from mcd_agent.commercial_portal import CommercialState, PortalClient
from mcd_agent.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the activated MCD host with Portal")
    parser.add_argument("--config", default="/opt/mcd/etc/mcd.toml")
    parser.add_argument("--local-identity", required=True)
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config, allow_recover_from_mcc=False)
    result = PortalClient(CommercialState(config.state_db_path)).register_host(
        local_identity=args.local_identity,
        display_name=args.display_name,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
