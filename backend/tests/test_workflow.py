"""端到端：用离线规则引擎走通六阶段流程，并验证人工闸门、介入级别与留痕约束。"""
from .conftest import DATA, login, run_jobs


def setup_users(c, h):
    for email, name, role in [("stu@x.org", "学生", "student"), ("men@x.org", "导师", "mentor")]:
        r = c.post("/api/users", json={"email": email, "name": name, "password": "pw123456", "role": role}, headers=h)
        assert r.status_code == 200, r.text
    return login(c, "stu@x.org", "pw123456"), login(c, "men@x.org", "pw123456")


def test_full_workflow(client):
    c = client
    admin = login(c)
    stu, men = setup_users(c, admin)

    # ---------------- 阶段一：命题与任务书、G0
    r = c.post("/api/projects", json={"name": "DCM 综述", "mentor_topic": "线粒体自噬与糖尿病心肌病", "mentor_email": "men@x.org"}, headers=stu)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    P = f"/api/projects/{pid}"
    assert r.json()["my_role"] == "student"

    # 学生不能自行调高 AI 介入级别
    assert c.put(f"{P}/settings", json={"ai_level_max": "L3"}, headers=stu).status_code == 403
    assert c.put(f"{P}/settings", json={"ai_level_max": "L2"}, headers=men).json()["ai_level_max"] == "L2"

    charter = {
        "mentor_topic": "线粒体自噬与糖尿病心肌病",
        "working_title": "Mitophagy in diabetic cardiomyopathy",
        "core_questions": ["Q1 糖尿病状态下心肌线粒体自噬如何改变", "Q2 PINK1/Parkin 与受体介导通路的证据强度"],
        "include": ["mitophagy in diabetic heart", "diabetic cardiomyopathy mice cardiomyocytes", "PINK1 Parkin FUNDC1 BNIP3"],
        "exclude": ["non-diabetic heart failure", "general autophagy without mitochondria-specific evidence"],
        "seed_ids": ["10000001", "10000002"],
        "concepts": [{"name": "diabetic cardiomyopathy", "terms": ["diabetic cardiomyopath*"], "mesh": ["Diabetic Cardiomyopathies"]}, {"name": "mitophagy", "terms": ["mitophag*", "PINK1"], "mesh": ["Mitophagy"]}],
    }
    r = c.post(f"{P}/charter", json={"data": charter}, headers=stu)
    assert r.status_code == 200 and r.json()["version"] == 1
    assert c.post(f"{P}/charter/1/submit", headers=stu).status_code == 200
    assert c.post(f"{P}/charter/1/decide", json={"decision": "approved"}, headers=stu).status_code == 403
    r = c.post(f"{P}/charter/1/decide", json={"decision": "approved", "comment": "同意"}, headers=men)
    assert r.json()["status"] == "approved" and r.json()["data"]["ai_level_max"] == "L2"
    # 批准后锁定：再次保存生成新版本
    assert c.post(f"{P}/charter", json={"data": charter}, headers=stu).json()["version"] == 2

    # P01 题目解构（规则引擎返回“需要配置模型”的提示，但调用被记录）
    r = c.post(f"{P}/ai/P01", json={"params": {}}, headers=stu)
    assert r.status_code == 200 and r.json()["run_id"].startswith("RUN-")

    # ---------------- 阶段二：检索式生成与语法校验
    r = c.post(f"{P}/search/generate", json={}, headers=stu)
    assert r.status_code == 200, r.text
    q = r.json()["data"]["query"]
    assert '"Diabetic Cardiomyopathies"[Mesh]' in q and r.json()["validation"]["ok"]
    bad = c.post(f"{P}/search/validate", json={"query": "TITLE-ABS-KEY(mitophagy)"}, headers=stu).json()
    assert not bad["ok"]
    sq = c.post(f"{P}/search/queries", json={"query": q, "origin": "ai_accepted"}, headers=stu).json()
    assert c.post(f"{P}/search/queries/{sq['id']}/final", headers=stu).json()["is_final"]
    # 离线模式：外部检索被拒绝，改用文件导入
    assert c.post(f"{P}/search/run", json={"query": q}, headers=stu).status_code == 503

    # ---------------- 阶段三：导入、去重、校准、辅助筛选
    with open(DATA / "sample.nbib", "rb") as f:
        r = c.post(f"{P}/records/import", files={"file": ("pubmed.nbib", f)}, data={"database": "PubMed", "query": q, "hit_count": "6"}, headers=stu)
    assert r.json()["added"] == 6
    with open(DATA / "sample.nbib", "rb") as f:
        r = c.post(f"{P}/records/import", files={"file": ("again.nbib", f)}, headers=stu)
    assert r.json()["added"] == 0 and r.json()["merged"] == 6

    # 未校准不能进入辅助模式
    assert c.post(f"{P}/screening/prescreen", headers=stu).status_code == 409
    c.post(f"{P}/screening/calibration/sample", headers=stu)
    cal = c.get(f"{P}/screening/calibration", headers=stu).json()
    assert len(cal) == 6 and "ai_decision" not in cal[0]  # 盲评
    assert c.post(f"{P}/screening/calibration/run-ai", headers=stu).status_code == 409
    truth = {"10000001": "include", "10000002": "include", "10000003": "include", "10000004": "exclude", "10000005": "exclude", "10000006": "exclude"}
    for rec in cal:
        c.put(f"{P}/records/{rec['id']}/calibration", json={"label": truth[rec["pmid"]]}, headers=stu)
    r = c.post(f"{P}/screening/calibration/run-ai", headers=stu)
    assert r.json()["prescreened"] == 6
    m = c.get(f"{P}/screening/calibration/metrics", headers=stu).json()
    assert m["n"] == 6 and m["sensitivity"] == 1.0, m
    assert c.post(f"{P}/screening/calibration/accept", headers=stu).json()["passed"]

    recs = c.get(f"{P}/records", params={"limit": 50}, headers=stu).json()["items"]
    by_pmid = {x["pmid"]: x for x in recs}
    assert by_pmid["10000006"]["ai_level"] == "U"  # 无摘要 → 信息不足，交人工
    assert by_pmid["10000001"]["seed" if False else "is_seed"]
    for pmid, d in truth.items():
        rr = c.post(f"{P}/records/{by_pmid[pmid]['id']}/decision", json={"decision": d}, headers=stu).json()
        assert rr["decision_source"] == "ai_then_human"
    prisma = c.get(f"{P}/prisma", headers=stu).json()
    assert prisma["included"] == 3 and prisma["excluded"] == 3 and prisma["identified_database_total"] == 6

    # ---------------- 阶段四：全文、证据卡、大纲（先写后看）、证据地图
    rid = by_pmid["10000001"]["id"]
    with open(DATA / "fulltext.md", "rb") as f:
        r = c.post(f"{P}/records/{rid}/fulltext", files={"file": ("zhang.md", f)}, headers=stu)
    assert r.json()["paragraphs"] == 4
    c.patch(f"{P}/records/{rid}", json={"relevance_level": "A"}, headers=stu)
    job = c.post(f"{P}/cards/extract", json={"record_ids": [rid]}, headers=stu).json()
    run_jobs()
    assert c.get(f"/api/jobs/{job['id']}", headers=stu).json()["status"] == "done"
    cards = c.get(f"{P}/cards", headers=stu).json()
    assert cards and all(cd["quote_found"] for cd in cards)
    assert all(cd["verify_status"] == "unverified" for cd in cards)

    assert c.get(f"{P}/evidence-map", headers=stu).status_code == 409  # 先想后看
    nodes = [
        {"id": "1", "title": "Introduction", "thesis": "为什么需要这篇综述", "level": 1},
        {"id": "2", "title": "Mitophagy changes", "thesis": "糖尿病心肌中线粒体自噬受损", "level": 1},
        {"id": "2.1", "title": "PINK1/Parkin", "thesis": "PINK1/Parkin 通路下调", "level": 2},
    ]
    assert c.post(f"{P}/outlines", json={"nodes": [{"id": "1.1.1", "title": "x", "level": 3}]}, headers=stu).status_code == 422
    o = c.post(f"{P}/outlines", json={"nodes": nodes}, headers=stu).json()
    assert o["kind"] == "student_draft"
    for cd in cards:
        c.patch(f"{P}/cards/{cd['card_id']}", json={"outline_nodes": ["2.1"], "verify_status": "verified"}, headers=stu)
    emap = c.get(f"{P}/evidence-map", headers=stu).json()
    n21 = next(n for n in emap["nodes"] if n["id"] == "2.1")
    assert n21["count"] == len(cards) and n21["a_count"] == 1 and n21["weak"]  # 少于 8 张
    assert "证据卡" in c.post(f"{P}/evidence-map/ai", headers=stu).json()["text"]
    c.post(f"{P}/gates/G3/submit", json={"object_version": f"outline@v{o['version']}"}, headers=stu)
    g = c.post(f"{P}/gates/G3/decide", json={"decision": "approved"}, headers=men).json()
    assert g["status"]["G3"]["status"] == "approved"
    pack = c.get(f"{P}/section-pack/2.1/text", headers=stu).text
    assert "PINK1" in pack

    # ---------------- 阶段五：写作（学生撰写）+ 审阅 + 语言建议（L3 才开放）
    ck = by_pmid["10000001"]["citekey"]
    para = c.post(f"{P}/paragraphs", json={"outline_node": "2.1", "thesis": "PINK1/Parkin 下调"}, headers=stu).json()
    text = f"In db/db mice, cardiac PINK1 and Parkin expression decreased by 45% at 16 weeks [@{ck}]. This proves mitophagy failure causes heart failure in all patients."
    r = c.put(f"{P}/paragraphs/{para['id']}", json={"spans": [{"text": text, "origin": "human"}]}, headers=stu).json()
    assert r["revision"] == 1 and r["provenance"]["human_ratio"] == 1.0
    assert c.post(f"{P}/paragraphs/{para['id']}/skeleton", headers=stu).status_code == 200  # L2
    rv = c.post(f"{P}/paragraphs/{para['id']}/review", headers=stu).json()
    cats = {a["body"]["category"] for a in rv["annotations"]}
    assert {"过度概括", "证据"} & cats
    assert c.post(f"{P}/paragraphs/{para['id']}/language", headers=stu).status_code == 403  # 需要 L3
    c.put(f"{P}/settings", json={"ai_level_max": "L3"}, headers=men)
    c.put(f"{P}/paragraphs/{para['id']}", json={"spans": [{"text": text + " In addition, this is very important.", "origin": "human"}]}, headers=stu)
    lang = c.post(f"{P}/paragraphs/{para['id']}/language", headers=stu).json()["annotations"]
    vi = next(a for a in lang if a["body"]["original"] == "very important")
    after = c.post(f"{P}/annotations/{vi['id']}", json={"action": "accept"}, headers=stu).json()
    assert after["status"] == "accepted"
    cur = c.get(f"{P}/paragraphs", headers=stu).json()[0]
    assert "critical" in cur["text"] and cur["provenance"]["ai_accepted"] == len("critical")

    # 大段粘贴提示
    long_paste = "x" * 300
    w = c.put(f"{P}/paragraphs/{para['id']}", json={"spans": cur["spans"] + [{"text": long_paste, "origin": "pasted"}]}, headers=stu).json()["warnings"]
    assert any("粘贴" in x for x in w)
    c.put(f"{P}/paragraphs/{para['id']}", json={"spans": cur["spans"]}, headers=stu)
    c.post(f"{P}/paragraphs/{para['id']}/done", headers=stu)

    # ---------------- 阶段六：断言核查、段落修改后退回、引文报告、声明、导出
    res = c.post(f"{P}/checks/run", json={}, headers=stu).json()["results"][0]
    assert res["claims"] >= 2
    checks = c.get(f"{P}/checks", headers=stu).json()["items"]
    first = next(x for x in checks if x["citekey"] == ck)
    assert first["ai_verdict"] == "strong", first
    assert "45%" in first["quote"]
    r = c.patch(f"{P}/checks/{first['check_id']}", json={"human_verdict": "strong"}, headers=stu)
    assert r.json()["status"] == "resolved"
    # 修改段落 → 核查自动退回
    c.put(f"{P}/paragraphs/{para['id']}", json={"spans": [{"text": text + " Edited.", "origin": "human"}]}, headers=stu)
    assert c.get(f"{P}/checks", headers=stu).json()["items"] == []
    assert c.patch(f"{P}/checks/{first['check_id']}", json={"human_verdict": "strong"}, headers=stu).status_code == 409

    rep = c.get(f"{P}/citations/report", headers=stu).json()
    assert rep["cited_total"] == 1 and rep["not_in_library"] == []
    d = c.post(f"{P}/disclosure/draft", json={"venue": "Cardiovasc Res"}, headers=stu).json()
    assert "作者对全部内容负责" in d["text"] and d["usage"]["runs"] > 0
    md = c.get(f"{P}/export/manuscript.md", headers=stu).text
    assert "## PINK1/Parkin" in md and f"[@{ck}]" in md
    assert f"@article{{{ck}," in c.get(f"{P}/export/references.bib", headers=stu).text
    assert c.get(f"{P}/search/log.csv", headers=stu).text.count("\n") >= 3

    # 审计日志：哈希链完整
    assert c.get("/api/admin/audit/verify", headers=admin).json()["ok"]
    assert len(c.get("/api/admin/audit", params={"project_id": pid}, headers=men).json()) > 20

    # 项目包：导出后导入为新项目
    pkg = c.get(f"{P}/export/package.zip", headers=stu).content
    r = c.post("/api/projects/import-package", files={"file": ("p.zip", pkg)}, headers=admin)
    assert r.status_code == 200, r.text
    new = r.json()["project_id"]
    assert c.get(f"/api/projects/{new}/records", headers=admin).json()["total"] == 6
    assert len(c.get(f"/api/projects/{new}/cards", headers=admin).json()) == len(cards)
    assert c.get("/api/admin/audit/verify", headers=admin).json()["ok"]


def test_data_local_only_forces_local_model(client):
    c = client
    h = login(c)
    pid = c.post("/api/projects", json={"name": "私密项目"}, headers=h).json()["id"]
    c.post("/api/admin/providers", json={"name": "deepseek", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "default_model": "deepseek-chat", "is_local": False}, headers=h)
    c.put("/api/admin/routes", json={"task": "*", "provider": "deepseek"}, headers=h)
    c.put(f"/api/projects/{pid}/settings", json={"data_local_only": True}, headers=h)
    r = c.post(f"/api/projects/{pid}/ai/P13", json={}, headers=h)
    assert r.status_code == 200 and r.json()["provider"] == "rules"  # 未调用云端
    providers = c.get("/api/admin/providers", headers=h).json()["configured"]
    assert providers[0]["has_key"] and "api_key_enc" not in providers[0]


def test_level_gate_and_permissions(client):
    c = client
    admin = login(c)
    stu, men = setup_users(c, admin)
    pid = c.post("/api/projects", json={"name": "L0 项目", "mentor_email": "men@x.org"}, headers=stu).json()["id"]
    c.put(f"/api/projects/{pid}/settings", json={"ai_level_max": "L0"}, headers=men)
    assert c.post(f"/api/projects/{pid}/ai/P10", json={}, headers=stu).status_code == 403
    assert c.post(f"/api/projects/{pid}/ai/P21", json={}, headers=stu).status_code == 200
    other = login(c, "admin@local", "admin")
    c.post("/api/users", json={"email": "x@x.org", "name": "外人", "password": "pw123456"}, headers=other)
    outsider = login(c, "x@x.org", "pw123456")
    assert c.get(f"/api/projects/{pid}", headers=outsider).status_code == 403
    avail = {p["id"]: p["enabled"] for p in c.get(f"/api/projects/{pid}/ai/available", headers=stu).json()}
    assert avail["P06"] and not avail["P10"]
