from __future__ import annotations


def _load_main():
    # Runtime prerequisites belong to the verified external bootstrap. An import
    # failure must never turn a normal agent invocation into an installer.
    from mcd_agent.cli import main
    return main


if __name__ == "__main__":
    raise SystemExit(_load_main()())
