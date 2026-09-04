import os, sys, tempfile, io

import os as _os; sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
os.environ["DEV_DB_PATH"] = tempfile.mktemp()
os.environ["EVIDENCE_STORE_PATH"] = tempfile.mkdtemp()

from app import create_app
from PIL import Image
import numpy as np

app = create_app()
failures = []


def check(label, resp, expect=200):
    ok = resp.status_code == expect
    print(("OK  " if ok else "FAIL"), label, resp.status_code, "(expected %s)" % expect if not ok else "")
    if not ok:
        failures.append((label, resp.status_code, resp.get_data(as_text=True)[-1500:]))
    return resp


def make_photo(seed):
    rng = np.random.RandomState(seed)
    arr = (rng.rand(300, 300, 3) * 255).astype("uint8")
    im = Image.fromarray(arr)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf


def csrf(c):
    return c.get("/auth/csrf-token").get_json()["csrf_token"]


def post_json(c, url, payload):
    return c.post(url, json=payload, headers={"X-CSRF-Token": csrf(c)})


seller = app.test_client()
admin = app.test_client()

check("register seller2", post_json(seller, "/auth/register", {
    "email": "seller2@example.com", "password": "password1", "display_name": "Verkoper Risico"}), 201)
check("become-seller2", post_json(seller, "/auth/become-seller", {}), 200)
check("register admin2", post_json(admin, "/auth/register", {
    "email": "admin2@example.com", "password": "password1", "display_name": "Beheerder2"}), 201)
from app.db import get_db
conn = get_db()
conn.execute("UPDATE users SET is_admin = 1 WHERE email = 'admin2@example.com'")
conn.commit()
conn.close()

r = post_json(seller, "/listings", {
    "brand": "Creed", "perfume_name": "Aventus", "size_ml": 100, "original_size_ml": 100,
    "estimated_remaining_percent": 90, "condition": "used_good", "asking_price_cents": 25000,
    "batch_code": "AAA111", "box_included": True, "description": "Test hoog risico.",
})
check("create high-risk listing", r, 201)
listing_id = r.get_json()["listing"]["id"]

# Upload all required except 'cap', and give the packaging batch code a
# DIFFERENT value than the bottle's -> batch_code_mismatch signal.
categories = [
    ("bottle_front", "AAA111"), ("bottle_back", None), ("bottle_bottom", None),
    ("nozzle", None), ("box_front", None), ("box_back", None), ("box_bottom", None),
    ("batch_code_bottle", "AAA111"), ("batch_code_packaging", "ZZZ999"),
]
for i, (cat, code) in enumerate(categories):
    photo = make_photo(100 + i)
    data = {"category": cat, "file": (photo, f"{cat}.jpg")}
    if code:
        data["seller_entered_code"] = code
    resp = seller.post(f"/listings/{listing_id}/photos", data=data,
                        content_type="multipart/form-data", headers={"X-CSRF-Token": csrf(seller)})
    check(f"upload {cat}", resp, 201)

r = post_json(seller, f"/listings/{listing_id}/submit-for-review", {})
check("submit high-risk for review", r, 200)
result = r.get_json()
print("verification_status:", result["verification_status"], "published:", result["published"])

conn = get_db()
row = conn.execute("SELECT risk_score, risk_band, verification_status FROM listings WHERE id = ?", (listing_id,)).fetchone()
conn.close()
print("risk_score:", row["risk_score"], "risk_band:", row["risk_band"])

check("web admin queue shows case", admin.get("/beheer"))
check("web admin review case page", admin.get(f"/beheer/advertenties/{listing_id}"))

if row["verification_status"] == "manual_review":
    # reject path
    r = post_json(admin, f"/admin/listings/{listing_id}/review",
                  {"action": "request_more_evidence", "notes": "Batchcode op fles en doos komen niet overeen — graag verduidelijken."})
    check("request_more_evidence", r, 200)
    check("web manage page after request_more_evidence", seller.get(f"/verkopen/{listing_id}"))

    # upload cap + fix batch code mismatch, resubmit
    photo = make_photo(999)
    resp = seller.post(f"/listings/{listing_id}/photos", data={"category": "cap", "file": (photo, "cap.jpg")},
                        content_type="multipart/form-data", headers={"X-CSRF-Token": csrf(seller)})
    check("upload cap on resubmit", resp, 201)
    r = post_json(seller, f"/listings/{listing_id}/submit-for-review", {})
    check("resubmit", r, 200)
    print("resubmit verification_status:", r.get_json()["verification_status"])

    conn = get_db()
    row = conn.execute("SELECT verification_status FROM listings WHERE id = ?", (listing_id,)).fetchone()
    conn.close()
    if row["verification_status"] == "manual_review":
        r = post_json(admin, f"/admin/listings/{listing_id}/review",
                      {"action": "escalate", "notes": None})
        check("escalate", r, 200)
        r = post_json(admin, f"/admin/listings/{listing_id}/review",
                      {"action": "reject", "notes": "Onvoldoende sluitend bewijs, definitief afgewezen voor test."})
        check("reject", r, 200)
        check("web review case after reject", admin.get(f"/beheer/advertenties/{listing_id}"))

print("\n=== SUMMARY 2 ===")
if failures:
    print(f"{len(failures)} FAILURES")
    for f in failures:
        print("---", f[0], f[1])
        print(f[2])
else:
    print("ALL CHECKS PASSED")
