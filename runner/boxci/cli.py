"""CLI entrypoint."""

import sys

from boxci import runner


def main() -> None:
    raise SystemExit(runner.main_cli(sys.argv[1:]))
