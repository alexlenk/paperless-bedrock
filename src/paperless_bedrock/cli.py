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
    from paperless_bedrock.consolidate import Consolidator
    from paperless_bedrock.service import Pipeline


def json_schema() -> str:
    """The analysis JSON Schema as committed in schema/letter-analysis-v1.schema.json."""
    return json.dumps(LetterAnalysis.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def _build() -> tuple[Settings, Pipeline, Consolidator]:
    from paperless_bedrock.config import Settings
    from paperless_bedrock.consolidate import Consolidator
    from paperless_bedrock.model import StrandsBedrockAnalyzer, StrandsBedrockJudge
    from paperless_bedrock.paperless import PaperlessClient
    from paperless_bedrock.service import Pipeline

    settings = Settings()  # required values come from the environment
    paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
    analyzer = StrandsBedrockAnalyzer(
        settings.bedrock_model_id, settings.aws_region, settings.max_output_tokens
    )
    judge = StrandsBedrockJudge(
        settings.judge_model_id or settings.bedrock_model_id, settings.aws_region
    )
    pipeline = Pipeline(settings, paperless, analyzer, judge)
    consolidator = Consolidator(
        paperless,
        pipeline.index,
        judge,
        max_judge_calls=settings.max_judge_calls_per_consolidation,
        hour=settings.consolidate_hour,
    )
    return settings, pipeline, consolidator


def serve(host: str, port: int) -> None:
    import uvicorn

    from paperless_bedrock.app import create_app
    from paperless_bedrock.jobs import Job, JobQueue, Worker

    settings, pipeline, consolidator = _build()
    queue = JobQueue(settings.data_dir / "jobs.sqlite3", recover=True)

    def process(job: Job) -> None:
        pipeline.run(job.document_id, light=job.light)

    worker = Worker(
        queue,
        process=process,
        on_failure=pipeline.mark_failed,
        max_attempts=settings.max_attempts,
        idle=consolidator.maybe_run,
    )
    app = create_app(
        queue, settings.webhook_token, on_startup=worker.start, on_shutdown=worker.stop
    )
    uvicorn.run(app, host=host, port=port, log_config=None)


def enqueue(args: argparse.Namespace) -> None:
    from paperless_bedrock.config import Settings
    from paperless_bedrock.jobs import PRIORITY_BATCH, JobQueue
    from paperless_bedrock.paperless import PaperlessClient

    settings = Settings()
    paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
    params: dict[str, object] = {"ordering": "-created"}
    if args.tag:
        tag_id = paperless.find_id("tags", args.tag)
        if tag_id is None:
            raise SystemExit(f"tag {args.tag!r} not found")
        params["tags__id__all"] = tag_id
    if args.created_after:
        params["created__date__gte"] = args.created_after
    if args.created_before:
        params["created__date__lte"] = args.created_before
    ids = (
        paperless.document_ids(params)[: args.limit]
        if args.limit
        else paperless.document_ids(params)
    )
    queue = JobQueue(settings.data_dir / "jobs.sqlite3")
    added = sum(queue.enqueue(i, light=args.light, priority=PRIORITY_BATCH) for i in ids)
    print(f"{len(ids)} documents matched, {added} queued (already analysed ones are skipped)")


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
    p_analyze.add_argument("--light", action="store_true", help="no PDF version")
    p_enqueue = sub.add_parser("enqueue", help="queue existing documents for the running server")
    p_enqueue.add_argument("--tag", help="only documents with this tag")
    p_enqueue.add_argument("--created-after", help="YYYY-MM-DD")
    p_enqueue.add_argument("--created-before", help="YYYY-MM-DD")
    p_enqueue.add_argument("--limit", type=int, help="at most this many (newest first)")
    p_enqueue.add_argument("--light", action="store_true", help="no PDF version (bulk imports)")
    p_consolidate = sub.add_parser("consolidate", help="merge duplicate correspondents/types now")
    p_consolidate.add_argument("--dry-run", action="store_true", help="only show what would merge")
    p_merges = sub.add_parser("merges", help="list recent automatic merges")
    p_merges.add_argument("--limit", type=int, default=30)
    p_undo = sub.add_parser("undo", help="undo an automatic merge")
    p_undo.add_argument("merge_id", type=int)
    sub.add_parser("reindex", help="rebuild the knowledge index from paperless notes")
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
    if args.command == "enqueue":
        enqueue(args)
        return 0

    from paperless_bedrock.consolidate import describe

    _, pipeline, consolidator = _build()
    if args.command == "analyze":
        outcome = pipeline.run(args.document_id, force=args.force, light=args.light)
        if outcome.analysis is None:
            print(f"document {args.document_id}: {outcome.status}")
        else:
            sys.stdout.write(outcome.analysis.model_dump_json(indent=2) + "\n")
    elif args.command == "consolidate":
        for plan in consolidator.run(dry_run=args.dry_run):
            print(describe(plan))
    elif args.command == "merges":
        for m in pipeline.index.merges(limit=args.limit):
            state = " (undone)" if m.undone_at else ""
            print(
                f"#{m.id} {m.kind}: {m.from_name!r} -> {m.into_name!r}, "
                f"{len(m.document_ids)} docs, {m.reason}{state}"
            )
    elif args.command == "undo":
        new_id = consolidator.undo(args.merge_id)
        print(f"merge {args.merge_id} undone (restored as id {new_id})")
    elif args.command == "reindex":
        print(f"{pipeline.reindex()} documents indexed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
