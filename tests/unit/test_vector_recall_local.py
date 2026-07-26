"""Local (fastembed) vector recall — semantic synonym bridge.

Resolves REAL semantic queries that keyword/FTS recall cannot (goldset xfail #4,#5:
pet≈cat, dark mode≈dark theme) via the local fastembed embedder.
SKIP if fastembed is not installed (CI/offline: vector is an upgrade, not a dependency).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastembed")  # skip if the local embedding model is unavailable

from akana.memory import Memory, OrchestratorSettings  # noqa: E402
from akana.memory.embed import LocalEmbedder  # noqa: E402
from akana.memory.vector_recall import enable_vector_recall  # noqa: E402

# fastembed is importable, but the ONNX model must also LOAD — a cleared/corrupt cache
# or an offline box makes embed() raise. Vector recall is an upgrade, not a dependency
# (see module docstring), so skip the module rather than fail the suite.
try:
    LocalEmbedder().embed(["probe"])
except Exception:  # ModelNotFoundError / EmbeddingError (download or ONNX load failure)
    pytest.skip(
        "fastembed model unavailable (cache cleared / offline) — vector recall skipped",
        allow_module_level=True,
    )

_FACTS = (
    ("kedi adı", "Pamuk", "user_statement"),
    ("köpek adı", "Karabaş", "user_statement"),
    ("preference:tema", "koyu tema kullanmayı severim", "user_statement"),
    ("preference:kahve", "sade filtre kahve içerim", "user_statement"),
    ("araba", "beyaz bir Egea kullanıyor", "inferred"),
    ("memleket", "İzmir", "user_statement"),
)


@pytest.fixture(scope="module")
def vec_orch(tmp_path_factory):
    mem = Memory.for_data_dir(tmp_path_factory.mktemp("vec-local"))
    for key, value, trust in _FACTS:
        mem.assert_fact_direct(key=key, value=value, trust=trust)
    orch = mem.make_orchestrator(
        settings=OrchestratorSettings(rate_limits={"memory.search": 10_000})
    )
    indexer = enable_vector_recall(mem, orch, LocalEmbedder())
    assert indexer is not None, "embedder verildi → vektör strategy'leri kaydedilmeli"
    indexer.reindex(mem)  # embed the existing facts (backfill)
    return orch


def _recalled(orch, query: str) -> str:
    items = orch.handle_tool_call("memory.search", {"query": query})["items"]
    return " ".join(i.get("summary", "") for i in items).lower()


def test_vector_bridges_evcil_hayvan_to_kedi(vec_orch) -> None:
    # the phrase 'evcil hayvan' does NOT appear in any key/value — only the semantic bridge finds it
    assert "pamuk" in _recalled(vec_orch, "evcil hayvanımın ismi ne")


def test_vector_bridges_karanlik_mod_to_koyu_tema(vec_orch) -> None:
    # 'karanlık mod' ≈ 'koyu tema' — impossible with keywords, the vector resolves it
    assert "koyu" in _recalled(vec_orch, "karanlık mod mu açık mod mu")


# The cosine floor, against the REAL model it was calibrated on. The two tests
# above are the "still recalls" direction; these two are the "stops recalling
# everything" direction. Both must hold — a floor that only satisfies one of
# them is either a leak or a silently emptied memory.


@pytest.mark.parametrize(
    "off_topic",
    [
        "quantum chromodynamics lattice gauge simulation",
        "kubernetes ingress controller tls termination",
        "what is the capital of Peru",
    ],
)
def test_off_topic_query_does_not_dump_the_fact_store(vec_orch, off_topic: str) -> None:
    # No fact in the corpus is about any of these; with no floor the vector leg
    # returned the whole table ranked by "least unrelated".
    assert _recalled(vec_orch, off_topic).strip() == ""


def test_english_query_still_bridges_to_a_turkish_fact(vec_orch) -> None:
    # Cross-language semantic recall must survive the floor (EN query, TR fact).
    assert "egea" in _recalled(vec_orch, "what car do I drive")
