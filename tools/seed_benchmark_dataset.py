"""Seeds an initial RAG Benchmark dataset from the REAL currently-uploaded
test knowledge files — every expected_file/expected_section/must_include
value below was read directly from the actual source files in knowledge/
(company-profile-test.md, graph-test.md, requirements (1).txt,
flexible-attachment-test.xlsx, preview-test.xlsx, full-auto-test.md), not
invented. See each case's "notes" for where its expected value came from.

Usage:
    python -m tools.seed_benchmark_dataset
"""
from ingestion.embedder import _get_supabase

DATASET_NAME = "Initial Real-Data Benchmark"

CASES = [
    # ── Shipify mission (company-profile-test.md > Our Mission) ──
    dict(question="What is Shipify's mission?", language="English",
         expected_answer="We aim to make cross-border shipping as easy as ordering food delivery.",
         expected_file="company-profile-test.md", expected_section="Our Mission",
         expected_answerability="direct_answer", must_include=["food delivery"], tags=["mission"]),
    dict(question="mission ของ Shipify คืออะไร", language="Mixed Thai-English",
         expected_answer="We aim to make cross-border shipping as easy as ordering food delivery.",
         expected_file="company-profile-test.md", expected_section="Our Mission",
         expected_answerability="direct_answer", must_include=["food delivery"], tags=["mission", "cross-language"]),
    dict(question="พันธกิจของ Shipify คืออะไร", language="Thai",
         expected_answer="We aim to make cross-border shipping as easy as ordering food delivery.",
         expected_file="company-profile-test.md", expected_section="Our Mission",
         expected_answerability="direct_answer", must_include=["food delivery"], tags=["mission", "cross-language"]),
    dict(question="เป้าหมายของ Shipify คืออะไร", language="Thai",
         expected_answer="We aim to make cross-border shipping as easy as ordering food delivery.",
         expected_file="company-profile-test.md", expected_section="Our Mission",
         expected_answerability="direct_answer", must_include=["food delivery"], tags=["mission", "cross-language"]),

    # ── Shipify services ──
    dict(question="What services does Shipify provide?", language="English",
         expected_answer="We provide warehouse consolidation, customs clearance, and last-mile delivery across Thailand.",
         expected_file="company-profile-test.md", expected_section="Our Services",
         expected_answerability="direct_answer", must_include=["warehouse", "customs clearance"], tags=["services"]),
    dict(question="Shipify มีบริการอะไรบ้าง", language="Thai",
         expected_answer="We provide warehouse consolidation, customs clearance, and last-mile delivery across Thailand.",
         expected_file="company-profile-test.md", expected_section="Our Services",
         expected_answerability="direct_answer", must_include=["warehouse", "customs clearance"],
         tags=["services", "cross-language"]),

    # ── Shipify contact ──
    dict(question="How can I contact Shipify?", language="English",
         expected_answer="You can reach our support team at support@shipify-example.com or visit our headquarters in Bangkok.",
         expected_file="company-profile-test.md", expected_section="Contact Us",
         expected_answerability="direct_answer", must_include=["support@shipify-example.com"], tags=["contact"]),
    dict(question="ติดต่อ Shipify ได้อย่างไร", language="Thai",
         expected_answer="You can reach our support team at support@shipify-example.com or visit our headquarters in Bangkok.",
         expected_file="company-profile-test.md", expected_section="Contact Us",
         expected_answerability="direct_answer", must_include=["support@shipify-example.com"],
         tags=["contact", "cross-language"]),
    dict(question="Where can I email Shipify support?", language="English",
         expected_answer="support@shipify-example.com", expected_file="company-profile-test.md",
         expected_section="Contact Us", expected_answerability="direct_answer",
         must_include=["support@shipify-example.com"], tags=["contact"]),

    # ── Google Drive dependencies (requirements (1).txt) ──
    dict(question="Google Drive dependencies มีอะไรบ้าง", language="Mixed Thai-English",
         expected_answer="google-api-python-client>=2.100.0, google-auth>=2.20.0",
         expected_file="requirements (1).txt", expected_section="Google Drive",
         expected_answerability="direct_answer", must_include=["google-api-python-client", "google-auth"],
         tags=["dependencies", "cross-language"]),
    dict(question="google-api-python-client ใช้ version อะไร", language="Mixed Thai-English",
         expected_answer="2.100.0", expected_file="requirements (1).txt", expected_section="Google Drive",
         expected_answerability="direct_answer", must_include=["2.100.0"], tags=["dependencies", "cross-language"]),
    dict(question="What is the minimum version of google-api-python-client?", language="English",
         expected_answer="2.100.0", expected_file="requirements (1).txt", expected_section="Google Drive",
         expected_answerability="direct_answer", must_include=["2.100.0"], tags=["dependencies"]),
    dict(question="google-auth ใช้ version อะไร", language="Mixed Thai-English",
         expected_answer="2.20.0", expected_file="requirements (1).txt", expected_section="Google Drive",
         expected_answerability="direct_answer", must_include=["2.20.0"], tags=["dependencies", "cross-language"]),
    dict(question="What is the minimum version of google-auth?", language="English",
         expected_answer="2.20.0", expected_file="requirements (1).txt", expected_section="Google Drive",
         expected_answerability="direct_answer", must_include=["2.20.0"], tags=["dependencies"]),

    # ── Negative controls — genuinely absent from every source file ──
    dict(question="Google Drive ต้องใช้ Python version อะไร", language="Mixed Thai-English",
         expected_answer=None, expected_file=None, expected_section=None,
         expected_answerability="no_information",
         must_not_include=["3.9", "3.10", "3.11", "3.12"], tags=["negative-control", "cross-language"],
         notes="requirements (1).txt lists package versions but never a Python interpreter version."),
    dict(question="What Python version is required for Google Drive?", language="English",
         expected_answer=None, expected_file=None, expected_section=None,
         expected_answerability="no_information", tags=["negative-control"],
         notes="Same as above — no Python interpreter version is specified anywhere in the source files."),
    dict(question="What operating system is required?", language="English",
         expected_answer=None, expected_file=None, expected_section=None,
         expected_answerability="no_information", tags=["negative-control"],
         notes="No OS requirement appears in any uploaded file."),
    dict(question="What is the database driver version?", language="English",
         expected_answer=None, expected_file=None, expected_section=None,
         expected_answerability="no_information", tags=["negative-control"],
         notes="requirements (1).txt has supabase>=2.4.0 (a client library, not a 'database driver version')."),

    # ── flexible-attachment-test.xlsx (real FAQ rows — these ARE
    #    answerable, unlike the negative controls above; kept distinct so
    #    the dataset covers both true negatives and Excel-Q&A retrieval) ──
    dict(question="What does the box look like?", language="English",
         expected_answer="See attached photo for reference.", expected_file="flexible-attachment-test.xlsx",
         expected_section="Question: What does the box look like?", expected_answerability="direct_answer",
         must_include=["photo"], tags=["excel-qa"]),
    dict(question="Any warranty doc?", language="English",
         expected_answer="Yes see the PDF.", expected_file=None, expected_section=None,
         expected_answerability="direct_answer", must_include=["warranty"], tags=["excel-qa", "ambiguous-source"],
         notes="Both flexible-attachment-test.xlsx ('Any warranty doc?') and preview-test.xlsx "
               "('Any warranty document?') answer this identically-phrased question — expected_file "
               "intentionally left blank since either source is a correct retrieval."),

    # ── preview-test.xlsx (real FAQ rows) ──
    dict(question="What is the shipping cost?", language="English",
         expected_answer="It depends on weight and destination.", expected_file="preview-test.xlsx",
         expected_section="Question: What is the shipping cost?", expected_answerability="direct_answer",
         must_include=["weight", "destination"], tags=["excel-qa"]),

    # ── full-auto-test.md ──
    dict(question="Does Shipify support automatic import without a preview step?", language="English",
         expected_answer="Shipify offers full auto import. This document verifies the pipeline runs "
                          "automatically without a preview step.",
         expected_file="full-auto-test.md", expected_section=None, expected_answerability="direct_answer",
         must_include=["automatic"], tags=["full-auto-import"]),

    # ── graph-test.md ──
    dict(question="Where is Shipify's China warehouse located?", language="English",
         expected_answer="Guangzhou", expected_file="graph-test.md", expected_section=None,
         expected_answerability="direct_answer", must_include=["Guangzhou"], tags=["graph-test"]),
    dict(question="Does Shipify support LINE as a customer service channel?", language="English",
         expected_answer="Yes, Shipify supports LINE OA as a customer service channel.",
         expected_file="graph-test.md", expected_section=None, expected_answerability="direct_answer",
         must_include=["LINE"], tags=["graph-test"]),
    dict(question="What countries does Shipify operate between?", language="English",
         expected_answer="China and Thailand", expected_file=None, expected_section=None,
         expected_answerability="direct_answer", must_include=["China", "Thailand"],
         tags=["graph-test", "company-profile"],
         notes="Both company-profile-test.md and graph-test.md state this — expected_file left blank."),
    dict(question="When was Shipify founded?", language="English",
         expected_answer="2020", expected_file="company-profile-test.md", expected_section="Shipify Company Profile",
         expected_answerability="direct_answer", must_include=["2020"], tags=["company-profile"]),
    dict(question="บริษัท Shipify ก่อตั้งเมื่อไหร่", language="Thai",
         expected_answer="2020", expected_file="company-profile-test.md", expected_section="Shipify Company Profile",
         expected_answerability="direct_answer", must_include=["2020"], tags=["company-profile", "cross-language"]),

    # ── requirements (1).txt — other real sections, generic version questions ──
    dict(question="What version of FastAPI is required?", language="English",
         expected_answer="0.109.0", expected_file="requirements (1).txt", expected_section="Web / API",
         expected_answerability="direct_answer", must_include=["0.109.0"], tags=["dependencies"]),
    dict(question="ระบบใช้ FastAPI version เท่าไหร่", language="Mixed Thai-English",
         expected_answer="0.109.0", expected_file="requirements (1).txt", expected_section="Web / API",
         expected_answerability="direct_answer", must_include=["0.109.0"], tags=["dependencies", "cross-language"]),
    dict(question="ต้องใช้ Supabase version อะไร", language="Mixed Thai-English",
         expected_answer="2.4.0", expected_file="requirements (1).txt",
         expected_section="Supabase (vector DB + user profiles)", expected_answerability="direct_answer",
         must_include=["2.4.0"], tags=["dependencies", "cross-language"]),
    dict(question="What version of the LINE bot SDK is used?", language="English",
         expected_answer="3.5.0", expected_file="requirements (1).txt", expected_section="LINE",
         expected_answerability="direct_answer", must_include=["3.5.0"], tags=["dependencies"]),
]


def main():
    sb = _get_supabase()
    existing = sb.table("rag_benchmark_datasets").select("id").eq("name", DATASET_NAME).execute().data
    if existing:
        dataset_id = existing[0]["id"]
        print(f"Reusing existing dataset {DATASET_NAME!r} ({dataset_id})")
    else:
        row = sb.table("rag_benchmark_datasets").insert({
            "name": DATASET_NAME,
            "description": "Seeded from real uploaded test files (company-profile-test.md, graph-test.md, "
                            "requirements (1).txt, flexible-attachment-test.xlsx, preview-test.xlsx, "
                            "full-auto-test.md) — no invented expected values.",
        }).execute().data[0]
        dataset_id = row["id"]
        print(f"Created dataset {DATASET_NAME!r} ({dataset_id})")

    inserted = 0
    for case in CASES:
        payload = dict(case)
        payload.setdefault("must_include", [])
        payload.setdefault("must_not_include", [])
        payload.setdefault("prohibited_files", [])
        payload.setdefault("tags", [])
        payload["dataset_id"] = dataset_id
        sb.table("rag_benchmark_cases").insert(payload).execute()
        inserted += 1

    print(f"Inserted {inserted} case(s) into dataset {dataset_id}.")


if __name__ == "__main__":
    main()
