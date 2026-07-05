"""Gold-set eval harness for recall quality (Vision F2) — regression guardrail.

INTENT
======
This file is a primitive but measurable guardrail for ``memory.search`` recall
quality:

* CORPUS: fixed ~28 Turkish semantic facts (personal info, preference, lesson;
  user_statement/inferred/tool_output trust tiers) +
  6 episodic turns across 3 conversations.
* GOLD_SET: 30 realistic Turkish queries. For each query an "expected" — the
  fact key/value or turn fragment that must appear in the summary/id of the
  top-5 results.
* CALIBRATED against today's keyword/FTS recall: the 2 xfail-marked queries are
  the GENUINELY SEMANTIC synonym queries that the current engine INTENTIONALLY
  misses (evcil hayvan≈kedi, karanlık mod≈koyu tema). Morphology (accusative/
  possessive: editörü→editör, takımı→takım, arabam→araba) used to be xfail;
  once Turkish stemming was extended they turned XPASS → promoted (floor 25→28).
  Once vector recall (F3) arrives via
  ``register_strategy("vector_first", ...)`` the remaining 2 xfails are expected
  to turn XPASS too; on that day the marks should be removed and the floor raised.
* Metrics: (a) hit@5 per query, (b) overall hit-rate on the non-xfail ones
  >= HIT_RATE_FLOOR, (c) trust gate sanity (tool_output does not leak at the
  default floor), (d) latency sanity (total of 30 queries < 5 s).

Measured actual run (keyword/FTS + extended Turkish stemming): 28/28 = 1.00.
Floor 0.88 was chosen — it tolerates at most 3 legitimate ranking shuffles
across 28 queries, while catching a systemic breakage instantly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from akana.memory import Memory, OrchestratorSettings

TOP_K = 5  # hit@K window
REQUEST_K = 5  # k passed to memory.search (recall limit)
HIT_RATE_FLOOR = 0.88  # measured 1.00; tolerates <=3 drops across 25 queries
LATENCY_BUDGET_S = 5.0  # total time for 30 queries (sanity, not a perf benchmark)

XFAIL_REASON = "vektör recall (F3) bekliyor"

# ---------------------------------------------------------------------------
# CORPUS — fixed; queries are calibrated against it. When changing it, re-verify
# the gold-set (key/value substrings affect LIKE matching).
# ---------------------------------------------------------------------------

# (key, value, trust)
CORPUS_FACTS: tuple[tuple[str, str, str], ...] = (
    # personal info (user_statement)
    ("kedi adı", "Pamuk", "user_statement"),
    ("köpek adı", "Karabaş", "user_statement"),
    ("doğum günü", "12 Mart 1990", "user_statement"),
    ("memleket", "İzmir", "user_statement"),
    ("kan grubu", "A Rh pozitif", "user_statement"),
    ("kardeş sayısı", "iki kardeşim var", "user_statement"),
    ("favori yemek", "mantı", "user_statement"),
    ("favori takım", "Göztepe", "user_statement"),
    ("ev şehri", "İstanbul Kadıköy'de oturuyor", "user_statement"),
    # preferences (with the preference: key prefix)
    ("preference:tema", "koyu tema kullanmayı severim", "user_statement"),
    ("preference:kahve", "sade filtre kahve içerim", "user_statement"),
    ("preference:bildirim", "sabah dokuzdan önce bildirim istemem", "user_statement"),
    ("preference:dil", "yanıtlar Türkçe olsun", "user_statement"),
    ("preference:müzik", "çalışırken enstrümantal müzik dinlerim", "user_statement"),
    ("preference:editör", "kod için neovim kullanırım", "user_statement"),
    # lessons (lesson: prefix, inferred)
    ("lesson:sqlite fts", "FTS5 bazı SQLite build'lerinde yok; LIKE fallback şart", "inferred"),
    ("lesson:venv", "testleri her zaman venv içindeki python ile çalıştır", "inferred"),
    ("lesson:migration", "veritabanı migration öncesi mutlaka yedek alınmalı", "inferred"),
    ("lesson:utf8", "log dosyaları utf-8 açılmalı, yoksa türkçe karakter bozulur", "inferred"),
    # work info
    ("proje deadline", "Ruflo v1 teslimi 15 Temmuz", "user_statement"),
    ("sprint ritmi", "iki haftalık sprintlerle çalışıyoruz", "user_statement"),
    ("standup saati", "her sabah 09:30'da standup yapılır", "user_statement"),
    ("yönetici", "takım lideri Ayşe", "user_statement"),
    # inferences (inferred)
    ("uyku düzeni", "gece geç saatte çalışmayı tercih ediyor", "inferred"),
    ("spor", "haftada üç gün koşuya çıkar", "inferred"),
    ("araba", "beyaz bir Egea kullanıyor", "inferred"),
    ("telefon modeli", "Pixel 8 kullanıyor", "inferred"),
    # low trust (tool_output) — MUST NOT leak into recall at the default floor (test c)
    ("tahmini konum", "ip kaydına göre Karşıyaka", "tool_output"),
    ("tahmini saat dilimi", "Europe/Istanbul UTC+3", "tool_output"),
)

# (conversation_id, role, text) — 3 conversations, 6 turns
CORPUS_TURNS: tuple[tuple[str, str, str], ...] = (
    ("c-bilet", "user", "dolmuş ücreti yirmi beş lira olmuş, pahalı geldi"),
    ("c-bilet", "assistant", "Evet, dolmuş zamlanmış; aylık kart daha mantıklı olabilir."),
    ("c-film", "user", "dün akşam Dune filmini izledik, görüntüler harikaydı"),
    ("c-film", "assistant", "Dune'un çöl sahneleri gerçekten etkileyici."),
    ("c-sunum", "user", "perşembe günü demo sunumu var, slaytları hazırlamam lazım"),
    ("c-sunum", "assistant", "Slayt taslağını bugün çıkaralım, demo perşembe 14:00'te."),
)


# ---------------------------------------------------------------------------
# GOLD_SET — (query, intent, expected). ``expected`` is lowercase; searched with
# casefold in the ``id + summary`` concatenation of each top-5 item.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gold:
    query: str
    intent: str | None
    expected: str  # lowercase substring (fact key/value or turn fragment)
    vector_only: bool = False  # True => not expected from today's keyword recall (xfail)
    note: str = ""


GOLD_SET: tuple[Gold, ...] = (
    # -- personal info ------------------------------------------------------
    Gold("kedimin adı neydi", "fact_lookup", "kedi adı"),
    Gold("köpeğimin adı ne", "fact_lookup", "karabaş"),
    Gold("doğum günüm ne zaman", "fact_lookup", "doğum günü"),
    Gold("memleketim neresi", "fact_lookup", "memleket"),
    Gold("kaç kardeşim var", "fact_lookup", "kardeş sayısı"),
    Gold("en sevdiğim yemek ne", "fact_lookup", "mantı"),
    Gold("nerede oturuyorum", "fact_lookup", "kadıköy"),
    Gold("telefonum hangi model", "fact_lookup", "pixel"),
    Gold("hangi araba kullanıyorum", "fact_lookup", "egea"),
    # -- preferences ----------------------------------------------------------
    Gold("tema tercihim ne", "fact_lookup", "koyu"),
    Gold("nasıl kahve içerim", "fact_lookup", "filtre kahve"),
    Gold("hangi dilde yanıt isterim", "fact_lookup", "preference:dil"),
    Gold("çalışırken ne dinlerim", "fact_lookup", "enstrümantal"),
    # -- lessons --------------------------------------------------------------
    Gold("sqlite dersi ne demişti", "lesson_lookup", "lesson:sqlite"),
    Gold("venv ile ilgili ders neydi", "lesson_lookup", "lesson:venv"),
    Gold("migration yaparken neye dikkat etmeliyim", "lesson_lookup", "yedek"),
    Gold("türkçe karakter bozulması için ders var mı", "lesson_lookup", "utf-8"),
    # -- work info ------------------------------------------------------------
    Gold("proje teslim tarihi ne zamandı", "fact_lookup", "15 temmuz"),
    Gold("sprint düzenimiz nasıl", "fact_lookup", "iki haftalık"),
    Gold("standup saat kaçta yapılıyor", "fact_lookup", "09:30"),
    Gold("yöneticim kim", "fact_lookup", "ayşe"),
    # -- inferences (intent diversity: explore) ------------------------------
    Gold("uyku düzenim hakkında ne biliyorsun", "explore", "uyku düzeni"),
    Gold("spor alışkanlığım ne", "fact_lookup", "koşu"),
    # -- episodic -------------------------------------------------------------
    Gold("dolmuş ücreti ne kadardı", "episodic", "yirmi beş lira"),
    Gold("hangi filmi izledik", "episodic", "dune"),
    Gold("demo sunumu ne zamandı", "episodic", "perşembe"),
    # -- morphology: now PASSES thanks to accusative/vowel-possessive stemming (was xfail) --
    Gold("hangi editörü kullanıyorum", "fact_lookup", "neovim"),
    Gold("hangi takımı tutuyorum", "fact_lookup", "göztepe"),
    Gold("arabam ne marka", "fact_lookup", "egea"),
    # -- xfail: genuine semantic synonym — awaiting vector recall (F3) --------
    Gold(
        "evcil hayvanımın ismi ne", "fact_lookup", "pamuk",
        vector_only=True, note="eşanlam: 'evcil hayvan'≈'kedi', 'ismi'≈'adı' köprüsü vektör ister",
    ),
    Gold(
        "karanlık mod mu açık mod mu", "fact_lookup", "koyu",
        vector_only=True, note="eşanlam: 'karanlık mod'≈'koyu tema' eşleşmesi keyword'le imkânsız",
    ),
)

_SCORED = tuple(g for g in GOLD_SET if not g.vector_only)  # the ones counted toward the hit-rate floor


def _params() -> list:
    out = []
    for i, g in enumerate(GOLD_SET):
        marks = (
            (pytest.mark.xfail(reason=f"{XFAIL_REASON}: {g.note}", strict=False),)
            if g.vector_only
            else ()
        )
        out.append(pytest.param(g, id=f"q{i:02d}", marks=marks))
    return out


# ---------------------------------------------------------------------------
# fixtures — the corpus is fixed and consumed read-only, so build it once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def memory(tmp_path_factory):
    mem = Memory.for_data_dir(tmp_path_factory.mktemp("goldset"))
    for key, value, trust in CORPUS_FACTS:
        mem.assert_fact_direct(key=key, value=value, trust=trust)
    for conv, role, text in CORPUS_TURNS:
        if role == "user":
            mem.remember_turn(role="user", conversation_id=conv, text=text)
        else:
            mem.remember_turn(role="assistant", conversation_id=conv, text=text)
    return mem


@pytest.fixture(scope="module")
def orch(memory):
    # The eval harness's query volume exceeds normal chat; deliberately raise the limit.
    settings = OrchestratorSettings(
        rate_limits={"memory.search": 100_000, "memory.remember": 60, "memory.forget": 30}
    )
    return memory.make_orchestrator(settings=settings)


def _search(orch, gold: Gold) -> list[dict]:
    args: dict = {"query": gold.query, "k": REQUEST_K}
    if gold.intent:
        args["intent"] = gold.intent
    out = orch.handle_tool_call("memory.search", args)
    assert "error" not in out, f"{gold.query!r}: {out.get('error')}"
    return out["items"]


@pytest.fixture(scope="module")
def gold_results(orch) -> dict[str, list[dict]]:
    """Each gold query runs once; hit@5 and hit-rate judge the same result."""
    return {g.query: _search(orch, g) for g in GOLD_SET}


def _is_hit(gold: Gold, items: list[dict]) -> bool:
    for item in items[:TOP_K]:
        hay = f"{item.get('id', '')} {item.get('summary', '')}".casefold()
        if gold.expected in hay:
            return True
    return False


# ---------------------------------------------------------------------------
# (a) hit@5 per query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gold", _params())
def test_hit_at_5(gold: Gold, gold_results):
    items = gold_results[gold.query]
    top = [i.get("summary") for i in items[:TOP_K]]
    assert _is_hit(gold, items), (
        f"sorgu {gold.query!r}: beklenen {gold.expected!r} top-{TOP_K} içinde yok; "
        f"dönen: {top}"
    )


# ---------------------------------------------------------------------------
# (b) overall hit-rate (excluding xfails) — the real regression guardrail
# ---------------------------------------------------------------------------


def test_overall_hit_rate_floor(gold_results):
    misses = [g.query for g in _SCORED if not _is_hit(g, gold_results[g.query])]
    rate = (len(_SCORED) - len(misses)) / len(_SCORED)
    assert rate >= HIT_RATE_FLOOR, (
        f"hit-rate {rate:.2f} < taban {HIT_RATE_FLOOR} "
        f"({len(misses)}/{len(_SCORED)} kaçtı): {misses}"
    )


# ---------------------------------------------------------------------------
# (c) trust gate sanity — tool_output does not leak at the default floor (inferred)
# ---------------------------------------------------------------------------


def test_trust_gate_tool_output_does_not_leak_by_default(orch):
    out = orch.handle_tool_call("memory.search", {"query": "tahmini konum", "k": REQUEST_K})
    assert "error" not in out
    leaked = [i["summary"] for i in out["items"] if "karşıyaka" in i["summary"].casefold()]
    assert leaked == [], f"tool_output fact default floor'u deldi: {leaked}"

    # positive control: when the floor is deliberately lowered the same fact should be visible
    lowered = orch.handle_tool_call(
        "memory.search", {"query": "tahmini konum", "k": REQUEST_K, "min_trust": "tool_output"}
    )
    assert "error" not in lowered
    assert any("karşıyaka" in i["summary"].casefold() for i in lowered["items"]), (
        "min_trust=tool_output ile gated fact görünmeliydi (gate testi körleşmiş olabilir)"
    )


# ---------------------------------------------------------------------------
# (d) latency sanity — total of 30 queries < 5 s (a guardrail, not a benchmark)
# ---------------------------------------------------------------------------


def test_latency_total_budget(orch):
    t0 = time.perf_counter()
    for gold in GOLD_SET:
        _search(orch, gold)
    elapsed = time.perf_counter() - t0
    assert elapsed < LATENCY_BUDGET_S, (
        f"{len(GOLD_SET)} gold sorgu {elapsed:.2f}s sürdü (bütçe {LATENCY_BUDGET_S}s)"
    )
