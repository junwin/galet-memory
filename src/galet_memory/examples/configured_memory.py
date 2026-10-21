"""Runnable public-config sample, including an explicitly named offline provider."""
from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files
import json
import math
from pathlib import Path
import re

from ..assembly import build_memory, galet_embedding_factory
from ..configuration import EmbeddingProfile, MemoryConfigurationError, load_memory_config


class OfflineSampleProvider:
    """Deterministic sample only; cannot label its vectors as a real provider."""
    def __init__(self, profile: EmbeddingProfile):
        if profile.space != ('sample', 'offline-hash-v1', 1536):
            raise MemoryConfigurationError('offline sample requires the sample/offline-hash-v1 profile')
        self.profile = profile

    def embed(self, texts, *, model):
        vectors = []
        for text in texts:
            vector = [0.0] * self.profile.dimensions
            for word in re.findall(r'\w+', text.casefold()):
                index = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], 'big') % len(vector)
                vector[index] += 1
            norm = math.sqrt(sum(v * v for v in vector))
            if not norm:
                vector[0], norm = 1.0, 1.0
            vectors.append([v / norm for v in vector])
        return vectors


def initialize_sample(directory: str | Path) -> Path:
    """Copy the installed JSON samples, replacing only machine-specific roots."""
    target = Path(directory).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=False)
    for name in ('data', 'data/digests', 'notes'):
        (target / name).mkdir(exist_ok=True)
    resources = files('galet_memory').joinpath('examples/config')
    for name in ('memory.json', 'roots.json', 'embedding-profiles.json'):
        (target / name).write_text(resources.joinpath(name).read_text(encoding='utf-8'), encoding='utf-8')
    roots = {'config_version': 1, 'external_roots': {
        'memory_data': str(target / 'data'), 'notes': str(target / 'notes')}}
    (target / 'roots.json').write_text(json.dumps(roots, indent=2) + '\n', encoding='utf-8')
    (target / 'notes/orchard.md').write_text('The orchard has apple trees and a quiet garden.\n', encoding='utf-8')
    return target / 'memory.json'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='otherwise use GALET_MEMORY_CONFIG_PATH')
    parser.add_argument('--init-sample', type=Path, help='create sample files in a NEW directory')
    parser.add_argument('--offline', action='store_true', help='use the explicitly named sample provider')
    parser.add_argument('--credential-path', help='Galet credential directory for a live profile')
    args = parser.parse_args(argv)
    try:
        if args.init_sample is not None:
            print(initialize_sample(args.init_sample))
            return 0
        config = load_memory_config(path=args.config)
        if config.semantic is None:
            raise MemoryConfigurationError('this sample requires semantic memory')
        if args.offline:
            factory = OfflineSampleProvider
        else:
            from galet.settings import Settings
            factory = galet_embedding_factory(Settings(credential_path=args.credential_path))
        with build_memory(config, embedding_provider_factory=factory) as memory:
            added = memory.ingest_file(account_name='sample', external_root='notes', relative_path='orchard.md')
            result = memory.recall(account_name='sample', query='orchard garden', score_threshold=0)
            fallback = memory.recall(account_name='sample', query='orchard', use_embeddings=False)
            print(json.dumps({'ingestion': added.status,
                'vector_matches': len(result.documents), 'keyword_matches': len(fallback.documents)}, indent=2))
        return 0
    except (MemoryConfigurationError, OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
