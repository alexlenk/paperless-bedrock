import datetime as dt
from pathlib import Path

from conftest import make_pdf
from fake_paperless import FakePaperless
from test_service import FakeJudge

from paperless_bedrock.consolidate import Consolidator, MergePlan
from paperless_bedrock.identifiers import Reference
from paperless_bedrock.index import KnowledgeIndex
from paperless_bedrock.model import JudgeResult
from paperless_bedrock.paperless import PaperlessClient

CREDITOR = Reference("creditor_id", "DE98ZZZ09999999999")


def setup(
    tmp_path: Path, judge: FakeJudge | None = None
) -> tuple[FakePaperless, KnowledgeIndex, Consolidator]:
    fake = FakePaperless(make_pdf([["x"]]))
    fake.documents = {
        1: {"id": 1, "correspondent": 10},
        2: {"id": 2, "correspondent": 10},
        3: {"id": 3, "correspondent": 11},
    }
    fake.objects["correspondents"] = {
        10: {"name": "Stadtwerke Musterstadt GmbH", "owner": None},
        11: {"name": "SWM Energie", "owner": None},
    }
    index = KnowledgeIndex(tmp_path / "k.db")
    for doc_id, corr in ((1, 10), (2, 10), (3, 11)):
        index.record_document(
            doc_id,
            correspondent_id=corr,
            document_type_id=None,
            sender_name="x",
            sender_address=None,
            subject="s",
            document_date=None,
            summary={},
            refs=[CREDITOR],
        )
    client = PaperlessClient("http://paperless", "secret", transport=fake.transport())
    return fake, index, Consolidator(client, index, judge or FakeJudge())


def test_identity_duplicates_are_merged_and_can_be_undone(tmp_path: Path) -> None:
    fake, index, consolidator = setup(tmp_path)
    plans = consolidator.run()

    assert [(p.source.name, p.target.name) for p in plans] == [
        ("SWM Energie", "Stadtwerke Musterstadt GmbH")
    ]
    assert fake.documents[3]["correspondent"] == 10
    assert 11 not in fake.objects["correspondents"]
    assert index.alias_target("correspondent", "SWM Energie") == 10
    merge = index.merges()[0]
    assert merge.document_ids == [3] and "DE98ZZZ09999999999" in merge.reason

    new_id = consolidator.undo(merge.id)
    assert fake.objects["correspondents"][new_id]["name"] == "SWM Energie"
    assert fake.documents[3]["correspondent"] == new_id
    assert index.alias_target("correspondent", "SWM Energie") is None
    assert index.is_blocked("correspondent", "SWM Energie", "Stadtwerke Musterstadt GmbH")
    # never merged again
    assert consolidator.plan("correspondent") == []


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    fake, _, consolidator = setup(tmp_path)
    assert len(consolidator.run(dry_run=True)) == 1
    assert fake.bulk_edits == [] and len(fake.objects["correspondents"]) == 2


def test_objects_created_by_a_person_are_kept(tmp_path: Path) -> None:
    fake, _, consolidator = setup(tmp_path)
    fake.objects["correspondents"][11]["owner"] = 2  # created by a person
    plans = consolidator.plan("correspondent")
    assert [(p.source.id, p.target.id) for p in plans] == [(10, 11)]

    fake.objects["correspondents"][10]["owner"] = 2
    assert consolidator.plan("correspondent") == []


def test_similar_names_merge_only_when_judge_is_confident(tmp_path: Path) -> None:
    fake = FakePaperless(make_pdf([["x"]]))
    fake.documents = {1: {"id": 1, "document_type": 20}, 2: {"id": 2, "document_type": 21}}
    fake.objects["document_types"] = {
        20: {"name": "Invoice", "owner": None},
        21: {"name": "Invoices", "owner": None},
        22: {"name": "Contract", "owner": None},
    }
    judge = FakeJudge(JudgeResult(match_id=21, confident=True, reason="plural"))
    client = PaperlessClient("http://paperless", "secret", transport=fake.transport())
    consolidator = Consolidator(client, KnowledgeIndex(tmp_path / "k.db"), judge)
    plans = consolidator.run()
    assert len(judge.requests) == 1  # "Contract" is not similar enough to be asked about
    assert [(p.source.name, p.target.name) for p in plans] == [("Invoices", "Invoice")]
    assert fake.documents[2]["document_type"] == 20

    judge_no = FakeJudge(JudgeResult(match_id=None, confident=False, reason="different"))
    fake.objects["document_types"][23] = {"name": "Invoice copy", "owner": None}
    assert (
        Consolidator(client, KnowledgeIndex(tmp_path / "k2.db"), judge_no).plan("document_type")
        == []
    )


def test_nightly_schedule(tmp_path: Path) -> None:
    _, _, consolidator = setup(tmp_path)
    runs: list[int] = []

    def fake_run(*, dry_run: bool = False) -> list[MergePlan]:
        runs.append(1)
        return []

    consolidator.run = fake_run  # type: ignore[method-assign]
    assert not consolidator.maybe_run(dt.datetime(2026, 10, 4, 2, 59))
    assert consolidator.maybe_run(dt.datetime(2026, 10, 4, 3, 0))
    assert not consolidator.maybe_run(dt.datetime(2026, 10, 4, 23, 0))
    assert consolidator.maybe_run(dt.datetime(2026, 10, 5, 4, 0))
    assert runs == [1, 1]
