"""Map the model's classification to paperless correspondents and document types."""

from __future__ import annotations

import logging
from typing import Literal

from rapidfuzz import fuzz

from paperless_bedrock.identifiers import Reference
from paperless_bedrock.index import KnowledgeIndex, norm_name
from paperless_bedrock.model import Judge, JudgeRequest
from paperless_bedrock.paperless import NamedObject, PaperlessClient
from paperless_bedrock.schema import Assignment

log = logging.getLogger(__name__)

Kind = Literal["correspondent", "document_type"]
ENDPOINTS: dict[str, str] = {"correspondent": "correspondents", "document_type": "document_types"}
CANDIDATE_SCORE = 80.0
MAX_CANDIDATES = 3


def similar(a: str, b: str) -> float:
    a, b = norm_name(a), norm_name(b)
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b), 0.95 * fuzz.token_set_ratio(a, b))


class Resolver:
    def __init__(self, paperless: PaperlessClient, index: KnowledgeIndex, judge: Judge) -> None:
        self.paperless = paperless
        self.index = index
        self.judge = judge

    def catalog(self, kind: Kind) -> list[NamedObject]:
        return self.paperless.list_objects(ENDPOINTS[kind])

    def resolve(
        self,
        kind: Kind,
        proposed: str,
        *,
        identity_refs: list[Reference] | None = None,
        profile: dict[str, object] | None = None,
    ) -> Assignment:
        proposed = " ".join(proposed.split())
        objects = self.catalog(kind)
        by_id = {o.id: o for o in objects}

        # 1. Identity references of the sender (VAT ID, creditor ID, register number)
        if kind == "correspondent" and identity_refs:
            ids = self.index.correspondents_for_identity(identity_refs) & by_id.keys()
            if len(ids) == 1:
                target = by_id[ids.pop()]
                self._remember_alias(kind, proposed, target)
                return Assignment(id=target.id, name=target.name, matched_by="identity_reference")

        # 2. Exact name or known alias
        for o in objects:
            if norm_name(o.name) == norm_name(proposed):
                return Assignment(id=o.id, name=o.name, matched_by="exact")
        alias_id = self.index.alias_target(kind, proposed)
        if alias_id in by_id:
            target = by_id[alias_id]
            return Assignment(id=target.id, name=target.name, matched_by="alias")

        # 3. Similar names: a model judges, without the letter text
        aliases = self.index.aliases(kind)
        scored = sorted(
            (
                (max(similar(proposed, n) for n in [o.name, *aliases.get(o.id, [])]), o)
                for o in objects
            ),
            key=lambda x: -x[0],
        )
        candidates = [
            o
            for score, o in scored[:MAX_CANDIDATES]
            if score >= CANDIDATE_SCORE and not self.index.is_blocked(kind, proposed, o.name)
        ]
        if candidates:
            result = self.judge.judge(
                JudgeRequest(
                    kind=kind,
                    new={"name": proposed, **(profile or {})},
                    candidates=[self._candidate(kind, o, aliases) for o in candidates],
                )
            )
            if result.confident and result.match_id in {o.id for o in candidates}:
                target = by_id[int(result.match_id)]
                log.info(
                    "%s %r matched to %r by judge: %s", kind, proposed, target.name, result.reason
                )
                self._remember_alias(kind, proposed, target)
                return Assignment(id=target.id, name=target.name, matched_by="judge")

        # 4. New object
        object_id = self.paperless.create_object(ENDPOINTS[kind], proposed)
        self.index.mark_created(kind, object_id)
        log.info("created %s %r (id %s)", kind, proposed, object_id)
        return Assignment(id=object_id, name=proposed, matched_by="created")

    def _candidate(
        self, kind: Kind, o: NamedObject, aliases: dict[int, list[str]]
    ) -> dict[str, object]:
        data: dict[str, object] = {"id": o.id, "name": o.name, "documents": o.document_count}
        if aliases.get(o.id):
            data["aliases"] = aliases[o.id]
        data.update(self.index.profile(kind, o.id))
        return data

    def _remember_alias(self, kind: Kind, alias: str, target: NamedObject) -> None:
        if norm_name(alias) != norm_name(target.name):
            self.index.add_alias(kind, alias, target.id)
