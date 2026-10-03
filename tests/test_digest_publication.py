import pytest

from galet_memory import CurationService, EpisodicEvent, SqliteEpisodicMemory
from galet_memory.curation import CurationStorageError, DigestPublicationError, CurationError
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
        assert len(index.records) == 2  # preview and immutable archive artifact
        assert len(memory.get_session("s", event_scope="all").events) == 2


def test_missing_publisher_does_not_append_boundary(tmp_path):
    with SqliteEpisodicMemory(tmp_path / "chat.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="agent", session_id="s")
        service = CurationService(memory, Generator())
        with pytest.raises(CurationStorageError, match="not configured"):
            service.archive(account_name="acct", session_id="s", publish=True)
        assert memory.get_session("s", event_scope="all").events == []


def test_publication_failure_can_retry_the_committed_boundary(tmp_path):
    class FlakyPublisher:
        def __init__(self):
            self.calls = []

        def publish(self, *, account_name, session_id, digest, digest_id=None, source_event_ids=()):
            self.calls.append(digest_id)
            if len(self.calls) == 1:
                raise RuntimeError("index unavailable")
            return type("Published", (), {"path": "published.md", "embedding_id": digest_id})()

    publisher = FlakyPublisher()
    generator = Generator()
    with SqliteEpisodicMemory(tmp_path / "chat.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="agent", session_id="s")
        memory.append_event("s", EpisodicEvent("user", "Hello."))
        service = CurationService(memory, generator, publisher)
        with pytest.raises(DigestPublicationError) as failure:
            service.archive(account_name="acct", session_id="s", idempotency_key="one", publish=True)
        boundary_id = failure.value.boundary_event_id
        assert len(memory.get_session("s", event_scope="all").events) == 2
        retried = service.retry_publication(account_name="acct", session_id="s",
                                            boundary_event_id=boundary_id)
        assert retried.boundary_event.event_id == boundary_id
        assert publisher.calls == [boundary_id, boundary_id]
        assert generator.calls == 1
        assert len(memory.get_session("s", event_scope="all").events) == 2


def test_cumulative_uses_committed_digests_and_reset_cuts_default_span(tmp_path):
    class CapturingGenerator:
        def __init__(self): self.inputs = []
        def generate(self, request):
            self.inputs.append([event.content for event in request.events])
            return f"Digest {len(self.inputs)}"

    generator = CapturingGenerator()
    with SqliteEpisodicMemory(tmp_path / "chat.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="agent", session_id="s")
        service = CurationService(memory, generator)
        memory.append_event("s", EpisodicEvent("user", "first"))
        first = service.archive(account_name="acct", session_id="s")
        memory.append_event("s", EpisodicEvent("user", "second"))
        second = service.archive(account_name="acct", session_id="s")
        cumulative = service.produce_cumulative_digest(account_name="acct", session_id="s")
        assert generator.inputs[-1] == [first.digest, second.digest]
        assert cumulative.source_event_ids == (first.boundary_event.event_id, second.boundary_event.event_id)
        assert len(memory.get_session("s", event_scope="all").events) == 4
        service.reset_context(account_name="acct", session_id="s")
        with pytest.raises(CurationError, match="no archived interval"):
            service.produce_cumulative_digest(account_name="acct", session_id="s")
        assert service.produce_cumulative_digest(account_name="acct", session_id="s", since_reset=False).source_event_ids == cumulative.source_event_ids
        memory.append_event("s", EpisodicEvent("user", "new topic"))
        latest = service.archive(account_name="acct", session_id="s")
        assert service.produce_cumulative_digest(account_name="acct", session_id="s").source_event_ids == (latest.boundary_event.event_id,)


def test_two_archives_publish_distinct_artifacts(tmp_path):
    index = Index()
    publisher = EmbeddingDigestPublisher(FilesystemDigestStore(tmp_path / "digests"), Embeddings(), index)
    with SqliteEpisodicMemory(tmp_path / "chat.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="agent", session_id="s")
        service = CurationService(memory, Generator(), publisher)
        memory.append_event("s", EpisodicEvent("user", "one"))
        first = service.archive(account_name="acct", session_id="s", publish=True)
        memory.append_event("s", EpisodicEvent("user", "two"))
        second = service.archive(account_name="acct", session_id="s", publish=True)
        assert first.publication.path != second.publication.path
        assert len(index.records) == 2
        assert first.boundary_event.metadata["previous_boundary_event_id"] is None
        assert second.boundary_event.metadata["previous_boundary_event_id"] == first.boundary_event.event_id
        cumulative = service.produce_cumulative_digest(account_name="acct", session_id="s", publish=True)
        assert cumulative.publication.path.endswith("s_cumulative.md")
        assert len(index.records) == 3


@pytest.mark.parametrize("component", ["..", "a/b", "a\\b", ""])
def test_document_store_rejects_path_escape(tmp_path, component):
    with pytest.raises(ValueError):
        FilesystemDigestStore(tmp_path).path_for(component, "s")
