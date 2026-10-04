"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TYPE_CHECKING

from paperless_bedrock import __version__
from paperless_bedrock.schema import LetterAnalysis

if TYPE_CHECKING:
    from paperless_bedrock.config import Settings
    from paperless_bedrock.service import Pipeline


def json_schema() -> str:
    """The analysis JSON Schema as committed in schema/letter-analysis-v1.schema.json."""
    return json.dumps(LetterAnalysis.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def _pipeline() -> tuple[Settings, Pipeline]:
    from paperless_bedrock.config import Settings
    from paperless_bedrock.model import StrandsBedrockAnalyzer
    from paperless_bedrock.paperless import PaperlessClient
    from paperless_bedrock.service import Pipeline

    settings = Settings()  # required values come from the environment
    paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
    analyzer = StrandsBedrockAnalyzer(
        settings.bedrock_model_id, settings.aws_region, settings.max_output_tokens
    )
    return settings, Pipeline(settings, paperless, analyzer)


def serve(host: str, port: int) -> None:
    import uvicorn

    from paperless_bedrock.app import create_app
    from paperless_bedrock.jobs import JobQueue, Worker

    settings, pipeline = _pipeline()
    queue = JobQueue(settings.data_dir / "jobs.sqlite3")
    worker = Worker(
        queue,
        process=pipeline.run,
        on_failure=pipeline.mark_failed,
        max_attempts=settings.max_attempts,
    )
    app = create_app(
        queue, settings.webhook_token, on_startup=worker.start, on_shutdown=worker.stop
    )
    uvicorn.run(app, host=host, port=port, log_config=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paperless-bedrock")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="print the analysis JSON Schema")
    p_serve = sub.add_parser("serve", help="run the webhook server and worker")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_analyze = sub.add_parser("analyze", help="analyze one document now and print the result")
    p_analyze.add_argument("document_id", type=int)
    p_analyze.add_argument("--force", action="store_true", help="re-analyze even if done")
    args = parser.parse_args(argv)

    if args.command == "schema":
        sys.stdout.write(json_schema())
        return 0

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.command == "serve":
        serve(args.host, args.port)
        return 0

    _, pipeline = _pipeline()
    outcome = pipeline.run(args.document_id, force=args.force)
    if outcome.analysis is None:
        print(f"document {args.document_id}: {outcome.status}")
    else:
        sys.stdout.write(outcome.analysis.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
