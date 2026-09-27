from reviewdesk.api.writing import apply_replacement, normalize_spans, provenance
from reviewdesk.gateway.prompts import all_prompts, extract_json, get_prompt, validate
from reviewdesk.services.library import make_citekey, parse_bibtex, parse_nbib, parse_ris
from reviewdesk.services.query_syntax import build_pubmed, convert, validate_pubmed
from reviewdesk.services.screening import calibration_metrics, calibration_sample_size
from reviewdesk.services.text import citekeys_in, quote_in, split_sentences

from .conftest import DATA


def codes(q):
    return {i.code for i in validate_pubmed(q).issues}


def test_pubmed_validator_rejects_scopus_syntax():
    r = validate_pubmed('TITLE-ABS-KEY(mitophagy W/3 heart) AND tumo?r')
    assert not r.ok
    assert {"scopus_field", "scopus_proximity", "wildcard_q"} <= codes('TITLE-ABS-KEY(mitophagy W/3 heart) AND tumo?r')


def test_pubmed_validator_accepts_spec_example():
    q = ('("Diabetic Cardiomyopathies"[Mesh] OR "diabetic cardiomyopath*"[tiab] OR "diabetic heart disease"[tiab]) '
         'AND ("Mitophagy"[Mesh] OR mitophag*[tiab] OR "mitochondrial autophagy"[tiab] OR PINK1[tiab])')
    assert validate_pubmed(q).ok


def test_pubmed_validator_proximity_rules():
    assert "prox_trunc" in codes('"insulin resist*"[tiab:~3]')
    assert "prox_field" in codes('"insulin resistance"[ab:~3]')
    assert validate_pubmed('"insulin resistance"[tiab:~3]').ok
    assert "short_trunc" in codes("can*[tiab]")
    assert "limit_in_query" in codes("mitophagy[tiab] AND Review[pt]")
    assert "parens" in codes("(mitophagy[tiab]")


def test_build_and_convert():
    q = build_pubmed([{"concept": "x", "mesh": ["Mitophagy"], "free_terms": ["mitophag*", "mitochondrial autophagy"]}])
    assert q == '(Mitophagy[Mesh] OR mitophag*[tiab] OR "mitochondrial autophagy"[tiab])'
    s = convert('"insulin resistance"[tiab:~3] AND Mitophagy[Mesh]', "scopus")
    assert "TITLE-ABS-KEY(insulin W/3 resistance)" in s["query"]
    assert s["unmapped"]
    w = convert("mitophag*[tiab]", "wos")
    assert w["query"] == "TS=(mitophag*)"
    c = convert("mitophagy[ti]", "cnki")
    assert c["query"] == "TI='mitophagy'"


def test_parsers():
    recs = parse_nbib((DATA / "sample.nbib").read_text())
    assert len(recs) == 6
    assert recs[0]["pmid"] == "10000001" and recs[0]["doi"] == "10.1000/cvr.2024.001"
    assert recs[0]["title"].endswith("db/db mice.")
    assert "Mitophagy" in recs[0]["mesh"]
    ris = parse_ris("TY  - JOUR\nTI  - 线粒体自噬与糖尿病心肌病\nAU  - 张三\nPY  - 2023\nER  - \n")
    assert ris[0]["source"] == "cnki" and ris[0]["year"] == 2023
    bib = parse_bibtex("@article{zhang2024mito, title={A {Big} title}, author={Zhang, Wei and Li, Na}, year={2024}, doi={10.1/x}}")
    assert bib[0]["citekey"] == "zhang2024mito" and bib[0]["authors"] == ["Zhang, Wei", "Li, Na"]
    assert make_citekey({"authors": ["Zhang W"], "year": 2024, "title": "The PINK1 story"}) == "zhang2024pink"


def test_calibration_metrics():
    m = calibration_metrics([("include", "include")] * 9 + [("include", "exclude")] + [("exclude", "exclude")] * 8 + [("exclude", "uncertain")] * 2)
    assert m["sensitivity"] == 0.9 and m["specificity"] == 0.8
    assert 0.6 < m["kappa"] < 0.8
    assert calibration_sample_size(50) == 50 and calibration_sample_size(3000) == 300


def test_text_utils():
    s = split_sentences("Mitophagy decreased [@zhang2024]. It was restored by FUNDC1 [@wang2023; @chen2022]. 这是中文。第二句！")
    assert len(s) == 4
    assert citekeys_in(s[1]) == ["wang2023", "chen2022"]
    assert quote_in("PINK1 and Parkin expression decreased by 45%", "cardiac PINK1 and Parkin expression decreased by 45 % at 16 weeks")


def test_prompt_registry_complete():
    ids = {p.id for p in all_prompts()}
    assert {f"P{i:02d}" for i in range(1, 22)} <= ids
    assert get_prompt("P15").min_level == "L2" and get_prompt("LANG").min_level == "L3"
    assert get_prompt("P06").render({"mentor_topic": "X", "concepts": "A、B"}).count("X") == 1
    data = extract_json('好的：\n```json\n[{"id": 1, "level": "A", "decision": "include", "reason": "r", "evidence_quote": "q"}]\n```')
    assert validate(data, get_prompt("P10").schema) == []
    assert validate([{"id": 1, "level": "Z", "decision": "include", "reason": "", "evidence_quote": ""}], get_prompt("P10").schema)


def test_spans_provenance_and_replacement():
    spans = normalize_spans([{"text": "Mitophagy is ", "origin": "human"}, {"text": "very important", "origin": "human"}, {"text": " here.", "origin": "pasted"}])
    assert len(spans) == 2
    out = apply_replacement(spans, "very important", "critical", "RUN-1")
    assert "".join(s["text"] for s in out) == "Mitophagy is critical here."
    pv = provenance(out)
    assert pv["ai_accepted"] == len("critical") and pv["pasted"] == len(" here.")


def test_pubmed_xml_jats_crossref_parsers():
    from reviewdesk.services.connectors import crossref_to_record, parse_jats, parse_pubmed_xml, retraction_status
    from reviewdesk.services.fulltext import parse_plain, parse_tei

    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
    <Journal><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue><Title>Cardiovasc Res</Title></Journal>
    <ArticleTitle>PINK1 in <i>db/db</i> mice</ArticleTitle>
    <Abstract><AbstractText Label="RESULTS">Down by 45%.</AbstractText></Abstract>
    <AuthorList><Author><LastName>Zhang</LastName><Initials>W</Initials></Author></AuthorList>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article>
    <MeshHeadingList><MeshHeading><DescriptorName>Mitophagy</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/x</ArticleId><ArticleId IdType="pmc">PMC9</ArticleId></ArticleIdList></PubmedData>
    </PubmedArticle></PubmedArticleSet>"""
    r = parse_pubmed_xml(xml)[0]
    assert (r["pmid"], r["doi"], r["pmcid"], r["year"], r["title"]) == ("123", "10.1/x", "PMC9", 2024, "PINK1 in db/db mice")
    assert r["abstract"] == "RESULTS: Down by 45%." and r["authors"] == ["Zhang W"] and r["mesh"] == ["Mitophagy"]

    jats = "<article><front><abstract><p>Abs.</p></abstract></front><body><sec><title>Results</title><p>Finding one.</p><sec><title>Sub</title><p>Two.</p></sec></sec></body></article>"
    paras = parse_jats(jats)
    assert [(p["id"], p["section"]) for p in paras] == [("p1", "Abstract"), ("p2", "Results"), ("p3", "Results / Sub")]

    tei = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><head>Results</head><p>Found X.</p></div></body></text></TEI>'
    assert parse_tei(tei) == [{"id": "p1", "section": "Results", "text": "Found X."}]
    assert parse_plain("# Results\n\nA finding.\n\nAnother.")[1]["section"] == "Results"

    work = {"DOI": "10.1/x", "title": ["T"], "author": [{"family": "Zhang", "given": "Wei"}], "issued": {"date-parts": [[2021]]}, "container-title": ["J"], "updated-by": [{"type": "retraction"}]}
    assert crossref_to_record(work)["authors"] == ["Zhang W"]
    assert retraction_status(work) == "retracted" and retraction_status({}) == "none"
