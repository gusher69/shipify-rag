"""One-off Production Cleanup (before LINE OA integration) — refreshes
the "Initial Real-Data Benchmark" dataset's stale expected_file/
expected_section/expected_answer/must_include values against the
CURRENT knowledge base (only "AI Knowledge Master.xlsx" has any active
chunks today; every file the dataset was originally written against —
company-profile-test.md, graph-test.md, requirements (1).txt,
flexible-attachment-test.xlsx, preview-test.xlsx, full-auto-test.md —
has been deleted/replaced).

Data-only fix. Never touches services/benchmark_service.py or
services/benchmark_metrics.py (no benchmark logic change) and never
invents a passing answer — cases whose topic no longer exists anywhere
in the current KB are converted to honest negative controls
(expected_answerability="no_information"), never silently deleted or
left pointing at a non-existent file.

Usage:
    python -m tools.refresh_benchmark_dataset_v1
"""
from admin.routes import get_sb

DATASET_ID = "188ccb41-f545-4834-807b-128ad62015fe"
CURRENT_FILE = "AI Knowledge Master.xlsx"

# Cases remapped to real, currently-active content (topic still exists,
# just with different specifics than the original test source).
REMAPPED = {
    "bef871e3-cf0d-4394-bf24-f470cbaaddea": {  # "What services does Shipify provide?"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "We offer ฝากสั่ง (buy-for-you), ฝากนำเข้า (import forwarding), and ฝากโอน (money transfer) services.",
        "must_include": [], "notes": "REMAPPED (Production Cleanup v1.0): original source (company-profile-test.md) "
                                      "no longer exists; current KB answers this via a different FAQ row (ฝากสั่ง/ฝากนำเข้า/ฝากโอน).",
    },
    "f551d5a0-eb4d-4a28-9269-8faeaebe1f8a": {  # "Shipify มีบริการอะไรบ้าง"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "มีบริการฝากสั่ง ฝากนำเข้า และฝากโอน",
        "must_include": ["ฝากสั่ง"],
        "notes": "REMAPPED (Production Cleanup v1.0): same as English variant — remapped to current FAQ content.",
    },
    "26633d59-0350-471d-b679-10c2a1ff7f04": {  # "How can I contact Shipify?"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "Shipify customer service: 02-026-6426; Fasttrade: 02-026-6425.",
        "must_include": [],
        "notes": "REMAPPED (Production Cleanup v1.0): original support@shipify-example.com email no longer "
                 "exists; current KB only has phone contact numbers.",
    },
    "154e3a59-e75a-4cff-b625-21ee2c948f48": {  # "ติดต่อ Shipify ได้อย่างไร"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "เบอร์ฝ่ายบริการลูกค้า 02-026-6426",
        "must_include": ["02-026-6426"],
        "notes": "REMAPPED (Production Cleanup v1.0): remapped to current phone-contact FAQ content.",
    },
    "0fca177c-01d6-43ba-98d9-32d85c31b445": {  # "What is the shipping cost?"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "Shipping cost is calculated from weight and volume (CBM).",
        "must_include": [],
        "notes": "REMAPPED (Production Cleanup v1.0): original preview-test.xlsx row is gone; current KB has "
                 "an equivalent shipping-rate/cost-calculation FAQ row. must_include left empty since the "
                 "current answer is Thai-language and exact English phrasing can't be asserted reliably.",
    },
    "7680d93b-ea87-4678-8fed-d712f4607e10": {  # "Where is Shipify's China warehouse located?"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "Customers are directed to the website's China warehouse address menu.",
        "must_include": [],
        "notes": "REMAPPED (Production Cleanup v1.0): original 'Guangzhou' fact is stale/no longer in the KB "
                 "(current answer directs the customer to a website menu instead of naming a city) — "
                 "must_include cleared since 'Guangzhou' would now be a FALSE assertion, never fabricated.",
    },
    "dd49182b-8254-4908-b468-b9007cac9483": {  # "What countries does Shipify operate between?"
        "expected_file": CURRENT_FILE, "expected_section": None,
        "expected_answer": "China and Thailand",
        "must_include": ["China", "Thailand"],
        "notes": "REMAPPED (Production Cleanup v1.0): still true and directly supported — every current "
                 "warehouse/shipping FAQ row concerns China<->Thailand shipping.",
    },
}

# Cases whose original topic no longer exists ANYWHERE in the current
# KB — converted to honest negative controls rather than left pointing
# at a deleted file or silently dropped.
STALE_TO_NEGATIVE_CONTROL = [
    "f315d580-ce9c-4922-8592-454bc6b9a567",  # What is Shipify's mission?
    "877e6034-e0d3-48d4-8b70-8694bebef7ca",  # mission ของ Shipify คืออะไร
    "bde564f7-6029-47e3-8243-3ce6f6437c50",  # พันธกิจของ Shipify คืออะไร
    "ede0b676-3cae-4c74-9398-8547c5a00856",  # เป้าหมายของ Shipify คืออะไร
    "68144935-2789-48e4-8998-d2c5b2d649c0",  # Where can I email Shipify support?
    "5b1979bd-ad72-4cb0-83f6-4b52b8b06faf",  # Google Drive dependencies มีอะไรบ้าง
    "b9238e36-bb4d-4f51-88ba-74bf70656a6d",  # google-api-python-client version
    "b9761807-b936-4a3a-85e6-d1052fbc6290",  # min version google-api-python-client
    "24f6da96-9398-4d5d-ab21-e13e8cfec680",  # google-auth version
    "624a34d1-16cb-43d5-8b9c-078f7892f509",  # min version google-auth
    "5bc06582-3c7b-458a-9d6f-e8cbd776b8b6",  # What does the box look like?
    "99972cce-5e54-43bd-a6b3-126767864427",  # Any warranty doc?
    "fa5ec7cc-2123-4938-be27-9a133d2ffcb4",  # full auto import feature
    "cb7a312d-22ed-416d-ba10-1e02a94adef9",  # Does Shipify support LINE as a customer service channel?
    "c38783e2-a8dd-4340-a356-1e22cce8451b",  # When was Shipify founded?
    "99040847-c005-47f5-9765-6a6f4abf6d4d",  # บริษัท Shipify ก่อตั้งเมื่อไหร่
    "9aa4bb38-8f49-48b3-9a7d-4086e16f34cd",  # FastAPI version
    "6087a8d3-441b-4bfd-9f17-21b38cbfa20c",  # FastAPI version (Thai)
    "865b988b-fb1c-428b-a8da-cc8ba447f306",  # Supabase version
    "391d3e12-9f8b-4f91-bfe4-8f4a631161fd",  # LINE bot SDK version
]

# Already-correct negative controls — untouched (never asserted a real
# file existed in the first place, so nothing about them is stale).
UNCHANGED = [
    "29449785-83e0-4a69-9223-bb4c84af2e4f",
    "80c9600d-79c5-49c9-bb21-4a12f214ed44",
    "d83ea180-2e98-4f46-ae85-5b62ec53d100",
    "e534b672-774e-43a1-96cf-06a6012a8b5e",
]


def main():
    sb = get_sb()
    updated = 0

    for case_id, patch in REMAPPED.items():
        sb.table("rag_benchmark_cases").update(patch).eq("id", case_id).execute()
        updated += 1
        print(f"REMAPPED  {case_id}")

    for case_id in STALE_TO_NEGATIVE_CONTROL:
        sb.table("rag_benchmark_cases").update({
            "expected_file": None, "expected_section": None,
            "expected_answerability": "no_information",
            "must_include": [], "expected_answer": None,
            "notes": "STALE (Production Cleanup v1.0): original source file no longer exists in the current "
                     "Knowledge Base ('AI Knowledge Master.xlsx' is the only active source today) — converted "
                     "to a negative control (no_information) rather than left pointing at a deleted file. "
                     "Question text preserved unchanged per 'preserve question quality'.",
        }).eq("id", case_id).execute()
        updated += 1
        print(f"NEGATIVE-CONTROL  {case_id}")

    print(f"\n{updated} case(s) updated, {len(UNCHANGED)} case(s) left unchanged (already-correct negative controls).")


if __name__ == "__main__":
    main()
