"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from paperless_bedrock import __version__
from paperless_bedrock.schema import LetterAnalysis


def json_schema() -> str:
    """The analysis JSON Schema as committed in schema/letter-analysis-v1.schema.json."""
    return json.dumps(LetterAnalysis.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paperless-bedrock")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="print the analysis JSON Schema")
    args = parser.parse_args(argv)

    if args.command == "schema":
        sys.stdout.write(json_schema())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
