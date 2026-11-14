import math
import random
from collections import Counter

import pytest

from backend.indexes.inverted_index import (
    InvertedIndex,
    InvertedIndexError,
    compile_match,
    difference,
    intersect,
    union,
)
from backend.storage.rid import RID
from backend.text.analyzer import Analyzer, normalize

WORDS = (
    "perro gato casa aprendizaje profundo basico redes neuronales datos sistema base motor disco indice "
    "arbol hash pagina memoria consulta rapido lento grande modelo vector imagen texto mapa"
).split()


def _docs(n: int, seed: int = 3) -> list[tuple[RID, str]]:
    rng = random.Random(seed)
    return [(RID(i // 10 + 1, i % 10), " ".join(rng.choices(WORDS, k=rng.randint(3, 30)))) for i in range(n)]


def _index(tmp_path, counter, docs, name="docs_cuerpo", block=5000, **kw):
    ix = InvertedIndex.create(tmp_path / name, 4096, counter, **kw)
    info = ix.build(iter(docs), block_postings=block)
    return ix, info


class Brute:
    """Modelo en memoria: mismos pesos TF-IDF y coseno, calculados directamente."""

    def __init__(self, docs, analyzer="spanish"):
        self.an = Analyzer(analyzer)
        self.rids = [rid for rid, _ in docs]
        self.tfs = [self.an.term_frequencies(text) for _, text in docs]
        self.n = len(docs)
        self.df = Counter()
        for tf in self.tfs:
            self.df.update(tf.keys())
        self.norms = [math.sqrt(sum(self.w(c, t) ** 2 for t, c in tf.items())) for tf in self.tfs]

    def w(self, tf, term):
        return (1 + math.log10(tf)) * math.log10(self.n / self.df[term])

    def ranked(self, query):
        qt = Counter(self.an.tokens(query))
        qn = math.sqrt(sum(self.w(c, t) ** 2 for t, c in qt.items() if t in self.df))
        out = []
        for i, tf in enumerate(self.tfs):
            if any(t in tf for t in qt):
                score = sum(self.w(qc, t) * self.w(tf[t], t) for t, qc in qt.items() if t in tf)
                out.append((score / (self.norms[i] * qn) if self.norms[i] and qn else 0.0, i))
        return sorted(out, key=lambda x: (-x[0], x[1]))


# ---------------------------------------------------------------------- preprocesamiento
def test_analizador_espanol():
    an = Analyzer("spanish")
    assert normalize("Camión ÑANDÚ") == "camion nandu"
    assert an.tokens("Los perros corrían rápidamente por la casa") == ["perr", "corri", "rapid", "cas"]
    assert an.term("de") is None and an.term("Aprendizaje") == "aprendizaj"
    assert an.term_frequencies("gato gatos GATO perro") == Counter({"gat": 3, "perr": 1})


def test_analizador_ingles_y_raw():
    assert Analyzer("english").tokens("The running shoes are blue") == ["run", "shoe", "blue"]
    raw = Analyzer("raw")
    assert raw.term_frequencies("v3:2 v17 v3") == Counter({"v3": 3, "v17": 1})
    with pytest.raises(ValueError):
        Analyzer("klingon")


# ---------------------------------------------------------------------- mezclas con dos punteros
def test_mezclas_con_dos_punteros():
    rng = random.Random(1)
    for _ in range(50):
        a = sorted(rng.sample(range(200), rng.randint(0, 80)))
        b = sorted(rng.sample(range(200), rng.randint(0, 80)))
        assert intersect(a, b) == sorted(set(a) & set(b))
        assert union(a, b) == sorted(set(a) | set(b))
        assert difference(a, b) == sorted(set(a) - set(b))


# ---------------------------------------------------------------------- construcción
def test_construccion_spimi_con_varios_bloques(tmp_path, counter):
    docs = _docs(3000)
    ix, info = _index(tmp_path, counter, docs, block=4000)
    brute = Brute(docs)
    assert info["runs"] > 3  # el corpus no cupo en un solo bloque
    assert info["documents"] == 3000 and info["terms"] == len(brute.df) == ix.num_terms
    assert info["postings"] == sum(brute.df.values())
    vocab = list(ix.iter_vocab())
    assert [t for t, _, _ in vocab] == sorted(brute.df)  # diccionario ordenado
    assert {t: df for t, df, _ in vocab} == dict(brute.df)
    # un solo bloque produce exactamente los mismos archivos
    single, _ = _index(tmp_path, counter, docs, name="uno", block=10**9)
    assert list(single.iter_vocab()) == vocab
    assert not (tmp_path / "docs_cuerpo.runs.tmp").exists()


def test_listas_de_posteo_ordenadas_por_doc_id(tmp_path, counter):
    docs = _docs(2000)
    ix, _ = _index(tmp_path, counter, docs, block=3000)
    brute = Brute(docs)
    for term in ("perr", "aprendizaj", "map"):
        df, offset = ix.lookup(term)
        postings = ix.read_postings(df, offset)
        doc_ids = postings["doc"].tolist()
        assert doc_ids == sorted(doc_ids) == [i for i, tf in enumerate(brute.tfs) if term in tf]
        assert postings["tf"].tolist() == [brute.tfs[i][term] for i in doc_ids]
    assert ix.lookup("inexistente") is None


def test_solo_se_leen_las_paginas_del_termino(tmp_path, counter):
    docs = _docs(6000)
    ix, _ = _index(tmp_path, counter, docs)
    df, offset = ix.lookup("perr")
    per = ix.postings_per_page
    counter.reset()
    ix.read_postings(df, offset)
    assert counter.disk_reads == (offset + df - 1) // per - offset // per + 1 < ix.postings.num_pages() - 1
    counter.reset()
    ix.lookup("perr")  # búsqueda binaria sobre las páginas del diccionario
    assert counter.disk_reads <= max(1, math.ceil(math.log2(ix.vocab.num_pages())))


# ---------------------------------------------------------------------- consultas
def test_ranking_coseno_igual_al_modelo(tmp_path, counter):
    docs = _docs(4000)
    ix, _ = _index(tmp_path, counter, docs, block=6000)
    brute = Brute(docs)
    for query in ("aprendizaje profundo redes", "perro gato", "mapa vector imagen texto modelo", "disco disco hash"):
        expected = brute.ranked(query)
        result = ix.search(query, k=10)
        assert result.mode == "ranked" and result.total == len(expected)
        assert [rid for rid, _ in result.hits] == [brute.rids[i] for _, i in expected[:10]]
        for (rid, score), (exp, _) in zip(result.hits, expected):
            assert score == pytest.approx(exp, abs=1e-6)
            assert 0.0 <= score <= 1.0 + 1e-6
    everything = ix.search("perro gato", k=None)
    assert len(everything.hits) == everything.total


def test_consultas_booleanas(tmp_path, counter):
    docs = _docs(3000)
    ix, _ = _index(tmp_path, counter, docs)
    brute = Brute(docs)
    a, p, b, g = "aprendizaj", "profund", "basic", "gat"

    def expect(pred):
        return [brute.rids[i] for i, tf in enumerate(brute.tfs) if pred(tf)]

    cases = {
        "aprendizaje AND profundo AND NOT basico": lambda tf: a in tf and p in tf and b not in tf,
        "aprendizaje OR gato": lambda tf: a in tf or g in tf,
        "(aprendizaje OR gato) AND NOT basico": lambda tf: (a in tf or g in tf) and b not in tf,
        "aprendizaje profundo AND gato": lambda tf: a in tf and p in tf and g in tf,  # yuxtaposición = AND
        "NOT basico": lambda tf: b not in tf,
        "gato AND NOT (aprendizaje OR profundo)": lambda tf: g in tf and not (a in tf or p in tf),
        "gato AND inexistente": lambda tf: False,
        "el AND gato": lambda tf: g in tf,  # las stopwords se ignoran
    }
    for query, pred in cases.items():
        result = ix.search(query, ranked=False)
        assert result.mode == "boolean"
        assert [rid for rid, _ in result.hits] == expect(pred), query
    ranked = ix.search("aprendizaje AND profundo AND NOT basico", k=5)
    assert ranked.total == len(expect(cases["aprendizaje AND profundo AND NOT basico"]))
    assert [s for _, s in ranked.hits] == sorted((s for _, s in ranked.hits), reverse=True)


def test_consultas_mal_formadas(tmp_path, counter):
    ix, _ = _index(tmp_path, counter, _docs(50))
    for bad in ("gato AND", "(gato OR perro", "gato )", "AND gato", "NOT"):
        with pytest.raises(InvertedIndexError):
            ix.search(bad)
    assert ix.search("zzz", k=5).hits == [] and ix.search("el la de", k=5).hits == []


def test_prefiltro_por_conjunto_de_rids(tmp_path, counter):
    docs = _docs(2000)
    ix, _ = _index(tmp_path, counter, docs)
    full = ix.search("perro gato casa", k=None)
    allowed = {rid for rid, _ in full.hits[::3]}
    filtered = ix.search("perro gato casa", k=20, allowed=allowed)
    assert filtered.total == len(allowed)
    assert [rid for rid, _ in filtered.hits] == [rid for rid, _ in full.hits if rid in allowed][:20]


def test_bm25(tmp_path, counter):
    docs = _docs(1500)
    ix, _ = _index(tmp_path, counter, docs, scoring="BM25")
    result = ix.search("aprendizaje profundo", k=10)
    assert result.scoring == "BM25" and len(result.hits) == 10
    assert [s for _, s in result.hits] == sorted((s for _, s in result.hits), reverse=True)
    with pytest.raises(InvertedIndexError):
        InvertedIndex.create(tmp_path / "x", 4096, counter, scoring="PAGERANK")


def test_bolsa_de_palabras_visuales(tmp_path, counter):
    docs = [(RID(1, 0), "v1:3 v2 v7"), (RID(1, 1), "v2:5 v9"), (RID(1, 2), "v1 v7:2 v8"), (RID(1, 3), "v4 v5")]
    ix, _ = _index(tmp_path, counter, docs, analyzer="raw")
    result = ix.search("v1:3 v2 v7", k=3)
    assert result.hits[0][0] == RID(1, 0) and result.hits[0][1] == pytest.approx(1.0, abs=1e-6)
    assert {rid for rid, _ in result.hits} == {RID(1, 0), RID(1, 1), RID(1, 2)}


def test_persistencia_y_reconstruccion(tmp_path, counter):
    docs = _docs(1200)
    ix, _ = _index(tmp_path, counter, docs)
    expected = ix.search("perro gato", k=7).hits
    ix.mark_stale()
    ix.close()
    again = InvertedIndex.open(tmp_path / "docs_cuerpo", 4096, counter)
    assert again.stale and again.num_docs == 1200
    with pytest.raises(InvertedIndexError):
        again.search("perro")
    again.build(iter(docs[:600]))
    assert not again.stale and again.num_docs == 600
    assert all(rid in {r for r, _ in docs[:600]} for rid, _ in again.search("perro gato", k=50).hits)
    again.build(iter(docs))
    assert again.search("perro gato", k=7).hits == expected


def test_busqueda_secuencial_equivalente(tmp_path, counter):
    docs = _docs(800)
    ix, _ = _index(tmp_path, counter, docs)
    an = Analyzer("spanish")
    for query in ("aprendizaje AND profundo AND NOT basico", "(perro OR gato) AND casa", "mapa imagen"):
        matcher = compile_match(query, an)
        scan = [rid for rid, text in docs if matcher(text)]
        assert sorted(scan) == sorted(rid for rid, _ in ix.search(query, ranked=False).hits)
