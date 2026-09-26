import pytest

from galet_memory import CurationService, EpisodicEvent, SqliteEpisodicMemory
from galet_memory.curation import CurationStorageError
from galet_memory.publication import EmbeddingDigestPublisher, FilesystemDigestStore


class Generator:
    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return "Summary: Hello. Welcome."


class Embeddings:
    def embed(self, texts, *, model):
        assert texts == ["Summary: Hello. Welcome."]
        assert model == "text-embedding-3-small"
        return [[0.1, 0.2, 0.3]]


class Index:
    def __init__(self):
        self.records = {}

    def upsert(self, embedding):
        self.records[embedding.id] = embedding


def test_preview_then_publish_and_archive_retry(tmp_path):
    index = Index()
    documents = FilesystemDigestStore(tmp_path / "digests")
    publisher = EmbeddingDigestPublisher(documents, Embeddings(), index)
    generator = Generator()
    with SqliteEpisodicMemory(tmp_path / "chat.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="agent", session_id="s")
        memory.append_event("s", EpisodicEvent("user", "Hello."))
        service = CurationService(memory, generator, digest_publisher=publisher)
        preview = service.produce_digest(account_name="acct", session_id="s")
        assert preview.publication is None
        assert not (tmp_path / "digests").exists()
        assert index.records == {}

        published = service.produce_digest(account_name="acct", session_id="s", publish=True)
        assert published.publication.path == str(tmp_path / "digests/acct/s.md")
        assert (tmp_path / "digests/acct/s.md").read_text() == published.digest
        record = index.records[published.publication.embedding_id]
        assert record.namespace == "digests"
        assert record.source_type == "digest"
        assert record.source_id == "s"
        assert record.metadata["path"] == published.publication.path

        archived = service.archive(account_name="acct", session_id="s", idempotency_key="op", publish=True)
        retried = service.archive(account_name="acct", session_id="s", idempotency_key="op", publish=True)
        assert archived.boundary_event.event_id == retried.boundary_event.event_id
        assert generator.calls == 3
        assert len(index.records) == 1
        assert len(memory.get_session("s", event_scope="all").events) == 2


def test_missing_publisher_does_not_append_boundary(tmp_path):
    with SqliteEpisodicMemory(tmp_path / "chat.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="agent", session_id="s")
        service = CurationService(memory, Generator())
        with pytest.raises(CurationStorageError, match="not configured"):
            service.archive(account_name="acct", session_id="s", publish=True)
        assert memory.get_session("s", event_scope="all").events == []


@pytest.mark.parametrize("component", ["..", "a/b", "a\\b", ""])
def test_document_store_rejects_path_escape(tmp_path, component):
    with pytest.raises(ValueError):
        FilesystemDigestStore(tmp_path).path_for(component, "s")
