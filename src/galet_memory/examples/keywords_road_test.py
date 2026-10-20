"""Run a keyword extraction road test with the default extractor."""

from __future__ import annotations

import json

from ..ports import SimpleKeywordExtractor


def main() -> int:
    extractor = SimpleKeywordExtractor()
    keywords = extractor.extract(
        "The orchard has apple trees. The apple trees need pruning before winter.",
        top_n=5,
    )
    assert "apple" in keywords and "trees" in keywords
    assert "the" not in keywords and "has" not in keywords
    assert len(keywords) == len(set(keywords))

    custom = SimpleKeywordExtractor(stopwords=("apple",))
    filtered = custom.extract("apple orchard orchard trees", top_n=2)
    assert filtered == ["orchard", "trees"]

    print(json.dumps({"keywords": keywords, "custom_stopwords": filtered,
                      "checks": "passed"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
