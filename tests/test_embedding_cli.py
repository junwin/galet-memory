from types import SimpleNamespace

from galet_memory.examples import embedding_cli
from galet_memory.ports import EmbeddingMatch, EmbeddingRecord


class FakeProvider:
    def embed(self, texts, *, model):
        return [[0.0] * 1536]


class FakeIndex:
    def __init__(self):
        self.stored = None

    def upsert(self, embedding):
        self.stored = embedding

    def query(self, **kwargs):
        return [
            EmbeddingMatch(
                EmbeddingRecord(
                    id="r1",
                    source_id="s1",
                    source_type="sample",
                    metadata={"text": "stored text"},
                ),
                0.91,
            )
        ]


def test_add_command_embeds_and_stores_text(monkeypatch, capsys):
    monkeypatch.setattr(
        embedding_cli, "_embedding_provider", lambda _: FakeProvider()
    )
    index = FakeIndex()
    args = SimpleNamespace(
        credential_path=None,
        text="stored text",
        model="text-embedding-3-small",
        source_id=None,
        record_id="r1",
        account="acct",
        namespace="demo",
        source_type="sample",
    )
    embedding_cli._add(args, index)
    assert index.stored.metadata == {"text": "stored text"}
    assert '"id": "r1"' in capsys.readouterr().out


def test_query_command_prints_matches(monkeypatch, capsys):
    monkeypatch.setattr(
        embedding_cli, "_embedding_provider", lambda _: FakeProvider()
    )
    args = SimpleNamespace(
        credential_path=None,
        text="query",
        model="text-embedding-3-small",
        account="acct",
        namespace="demo",
        limit=5,
        source_type=None,
    )
    embedding_cli._query(args, FakeIndex())
    output = capsys.readouterr().out
    assert '"score": 0.91' in output
    assert '"text": "stored text"' in output
