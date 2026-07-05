"""Turkish case-folding regressions — curator contradiction detection.

Python's locale-blind ``str.lower`` mangles Turkish dotted/dotless I:
``"İSTANBUL".lower()`` keeps a combining dot (``i̇stanbul``) and ``"I".lower()``
gives ``i`` instead of ``ı``. Before :func:`akana.memory.terms.fold_text` the
curator flagged ``"İstanbul" vs "istanbul"`` as a contradiction (invalidating
a valid fact) and would have re-staged ``"İZMİR"`` next to a known ``"İzmir"``.

``Curator.capture``/``stage_candidates`` (the promoted-fact-level dedup this
file used to exercise via ``_is_known``) were removed as dead code — the live
LLM-capture flow (``akana_server/memory_capture.py``) and the chat loop
(``akana_server/api/routes/chat/persist.py``) both stage candidates directly
via ``memory.staging.stage`` and rely on the LLM's own view of existing keys
for that dedup instead. This file now stages via ``memory.staging.stage``
directly and covers what promotion still guarantees: Turkish-fold-aware
contradiction detection (in ``SemanticStore.assert_fact``, driven by
``Curator.promote``) — a case variant must not open a second valid row, while a
genuinely different value supersedes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory import Memory
from akana.memory.curator import Curator
from akana.memory.staging import FactCandidate


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def curator(mem: Memory) -> Curator:
    return mem.make_curator()


# -- curator: no false contradictions ----------------------------------------


def test_istanbul_case_variant_is_not_contradiction(mem: Memory, curator: Curator) -> None:
    curator.promote(mem.staging.stage(FactCandidate(key="şehir", value="İstanbul")).id)

    # Case variants must NOT supersede the valid "İstanbul" fact (Turkish-folded).
    curator.promote(mem.staging.stage(FactCandidate(key="şehir", value="istanbul")).id)
    curator.promote(mem.staging.stage(FactCandidate(key="şehir", value="İSTANBUL")).id)
    assert [f.value for f in mem.semantic.facts_for_key("şehir")] == ["İstanbul"]

    # a genuinely different value still supersedes.
    curator.promote(mem.staging.stage(FactCandidate(key="şehir", value="Ankara")).id)
    assert [f.value for f in mem.semantic.facts_for_key("şehir")] == ["Ankara"]


def test_isik_dotless_variant_is_not_contradiction(mem: Memory, curator: Curator) -> None:
    curator.promote(mem.staging.stage(FactCandidate(key="tercih", value="IŞIK")).id)
    # old code: "IŞIK".lower() == "ışik" != "ışık" → false contradiction
    curator.promote(mem.staging.stage(FactCandidate(key="tercih", value="ışık")).id)
    assert [f.value for f in mem.semantic.facts_for_key("tercih")] == ["IŞIK"]


def test_promoting_case_variant_does_not_invalidate(mem: Memory, curator: Curator) -> None:
    staged = mem.staging.stage(FactCandidate(key="şehir", value="İstanbul"))
    assert curator.promote(staged.id) is not None

    # Stage + promote a case variant: it must NOT supersede/invalidate the
    # valid "İstanbul" fact (assert_fact folds Turkish case, so this is not
    # treated as a conflicting value).
    variant = mem.staging.stage(FactCandidate(key="şehir", value="istanbul"))
    curator.promote(variant.id)

    history = mem.semantic.facts_for_key("şehir", include_invalidated=True)
    assert history and all(f.is_valid for f in history)
    assert "İstanbul" in {f.value for f in history}
