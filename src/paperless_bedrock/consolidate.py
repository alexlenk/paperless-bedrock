"""Automatic consolidation of duplicate correspondents and document types, with undo."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from itertools import combinations

from paperless_bedrock.index import KnowledgeIndex
from paperless_bedrock.model import Judge, JudgeRequest
from paperless_bedrock.paperless import NamedObject, PaperlessClient
from paperless_bedrock.resolve import CANDIDATE_SCORE, ENDPOINTS, Kind, similar

log = logging.getLogger(__name__)

BULK_METHOD = {"correspondent": "set_correspondent", "document_type": "set_document_type"}
FILTER = {"correspondent": "correspondent__id", "document_type": "document_type__id"}


@dataclass(frozen=True)
class MergePlan:
    kind: Kind
    source: NamedObject  # will be merged away
    target: NamedObject  # remains
    reason: str


class Consolidator:
    def __init__(
        self,
        paperless: PaperlessClient,
        index: KnowledgeIndex,
        judge: Judge,
        *,
        max_judge_calls: int = 50,
        hour: int | None = 3,
    ) -> None:
        self.paperless = paperless
        self.index = index
        self.judge = judge
        self.max_judge_calls = max_judge_calls
        self.hour = hour
        self._last_run: dt.date | None = None

    # --- scheduling ----------------------------------------------------------------------

    def maybe_run(self, now: dt.datetime | None = None) -> bool:
        """Run once per day after the configured local hour. Called by the idle worker."""
        if self.hour is None:
            return False
        now = now or dt.datetime.now()
        if now.hour < self.hour or self._last_run == now.date():
            return False
        self._last_run = now.date()
        self.run()
        return True

    # --- consolidation -------------------------------------------------------------------

    def run(self, *, dry_run: bool = False) -> list[MergePlan]:
        self.sync_assignments()
        plans = self.plan("correspondent") + self.plan("document_type")
        if not dry_run:
            for p in plans:
                self.merge(p)
        log.info("consolidation: %d merges%s", len(plans), " (dry run)" if dry_run else "")
        return plans

    def sync_assignments(self) -> None:
        """Take paperless as the truth: someone may have reassigned documents by hand."""
        for row in self.paperless.document_fields(["id", "correspondent", "document_type"]):
            if row.get("correspondent") is not None:
                self.index.set_document_field("correspondent", [row["id"]], row["correspondent"])
            if row.get("document_type") is not None:
                self.index.set_document_field("document_type", [row["id"]], row["document_type"])

    def plan(self, kind: Kind) -> list[MergePlan]:
        objects = {o.id: o for o in self.paperless.list_objects(ENDPOINTS[kind])}
        ours = self.index.created_by_us(kind)
        plans: list[MergePlan] = []
        merged: set[int] = set()

        def protected(o: NamedObject) -> bool:
            # Created by a person (has an owner, not created by this tool): never merged away.
            return o.owner is not None and o.id not in ours

        def add(a: NamedObject, b: NamedObject, reason: str) -> None:
            if a.id in merged or b.id in merged or self.index.is_blocked(kind, a.name, b.name):
                return
            if protected(a) and protected(b):
                log.info("not merging %r and %r: both created by a person", a.name, b.name)
                return
            source, target = sorted((a, b), key=lambda o: (protected(o), o.document_count, -o.id))
            plans.append(MergePlan(kind, source, target, reason))
            merged.add(source.id)

        # 1. Same identity reference (VAT ID, creditor ID, register number): certain.
        if kind == "correspondent":
            for value, ids in self.index.identity_groups():
                present = sorted((objects[i] for i in ids if i in objects), key=lambda o: o.id)
                for other in present[1:]:
                    add(present[0], other, f"same identity reference {value}")

        # 2. Similar names: a model decides, with what the index knows about both.
        aliases = self.index.aliases(kind)
        pairs = sorted(
            (
                (similar(a.name, b.name), a, b)
                for a, b in combinations(objects.values(), 2)
                if not self.index.is_blocked(kind, a.name, b.name)
            ),
            key=lambda x: -x[0],
        )
        calls = 0
        for score, a, b in pairs:
            if score < CANDIDATE_SCORE or calls >= self.max_judge_calls:
                break
            if a.id in merged or b.id in merged:
                continue
            calls += 1
            result = self.judge.judge(
                JudgeRequest(
                    kind=kind,
                    new=self._profile(kind, a, aliases),
                    candidates=[self._profile(kind, b, aliases)],
                )
            )
            if result.confident and result.match_id == b.id:
                add(a, b, f"same entity (judge): {result.reason}")
        return plans

    def _profile(
        self, kind: Kind, o: NamedObject, aliases: dict[int, list[str]]
    ) -> dict[str, object]:
        data: dict[str, object] = {"id": o.id, "name": o.name, "documents": o.document_count}
        if aliases.get(o.id):
            data["aliases"] = aliases[o.id]
        data.update(self.index.profile(kind, o.id))
        return data

    def merge(self, plan: MergePlan) -> int:
        kind, source, target = plan.kind, plan.source, plan.target
        doc_ids = self.paperless.document_ids({FILTER[kind]: source.id})
        self.paperless.bulk_edit(doc_ids, BULK_METHOD[kind], {kind: target.id})
        merge_id = self.index.log_merge(
            kind, source.id, source.name, target.id, target.name, doc_ids, plan.reason
        )
        self.index.retarget_aliases(kind, source.id, target.id)
        self.index.add_alias(kind, source.name, target.id)
        self.index.move_documents(kind, source.id, target.id)
        self.paperless.delete_object(ENDPOINTS[kind], source.id)
        log.info(
            "merged %s %r into %r (%d documents): %s",
            kind,
            source.name,
            target.name,
            len(doc_ids),
            plan.reason,
        )
        return merge_id

    def undo(self, merge_id: int) -> int:
        """Recreate the merged-away object, move its documents back, never merge the pair again."""
        m = self.index.get_merge(merge_id)
        if m is None:
            raise ValueError(f"merge {merge_id} not found")
        if m.undone_at is not None:
            raise ValueError(f"merge {merge_id} was already undone")
        kind: Kind = "correspondent" if m.kind == "correspondent" else "document_type"
        new_id = self.paperless.create_object(ENDPOINTS[kind], m.from_name)
        self.index.mark_created(kind, new_id)
        still_there = set(self.paperless.document_ids({FILTER[kind]: m.into_id}))
        back = [d for d in m.document_ids if d in still_there]
        self.paperless.bulk_edit(back, BULK_METHOD[kind], {kind: new_id})
        self.index.set_document_field(kind, back, new_id)
        self.index.remove_alias(kind, m.from_name)
        self.index.block(kind, m.from_name, m.into_name)
        self.index.mark_undone(merge_id)
        log.info("undid merge %s: %r restored with %d documents", merge_id, m.from_name, len(back))
        return new_id


def describe(plan: MergePlan) -> str:
    return (
        f"{plan.kind}: {plan.source.name!r} ({plan.source.document_count} docs) -> "
        f"{plan.target.name!r} ({plan.target.document_count} docs): {plan.reason}"
    )
