import os, sys, tempfile, io, json

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


# ---------------------------------------------------------------------
seller = app.test_client()
buyer = app.test_client()
admin = app.test_client()
anon = app.test_client()

check("register seller", post_json(seller, "/auth/register", {
    "email": "seller@example.com", "password": "password1", "display_name": "Verkoper Sanne"}), 201)
check("become-seller", post_json(seller, "/auth/become-seller", {}), 200)

check("register buyer", post_json(buyer, "/auth/register", {
    "email": "buyer@example.com", "password": "password1", "display_name": "Koper Bram"}), 201)

check("register admin", post_json(admin, "/auth/register", {
    "email": "admin@example.com", "password": "password1", "display_name": "Beheerder"}), 201)
# flip is_admin directly in DB (no self-service admin endpoint by design)
from app.db import get_db
conn = get_db()
conn.execute("UPDATE users SET is_admin = 1 WHERE email = 'admin@example.com'")
conn.commit()
conn.close()
# refresh admin session's csrf/user (already logged in via register)

# --- Web pages: anonymous ---
check("web / anon", anon.get("/"))
check("web /verkopen anon", anon.get("/verkopen"))
check("web /inloggen", anon.get("/inloggen"))
check("web /registreren", anon.get("/registreren"))

# --- Sell flow ---
check("web /verkopen seller (needs seller ok)", seller.get("/verkopen"))
r = post_json(seller, "/listings", {
    "brand": "Dior", "perfume_name": "Sauvage", "size_ml": 100, "original_size_ml": 100,
    "estimated_remaining_percent": 95, "condition": "used_good", "asking_price_cents": 6000,
    "batch_code": "ABC123", "box_included": True, "description": "Nauwelijks gebruikt.",
})
check("create listing", r, 201)
listing_id = r.get_json()["listing"]["id"]

check("web /verkopen/<id> manage", seller.get(f"/verkopen/{listing_id}"))

categories = ["bottle_front", "bottle_back", "bottle_bottom", "nozzle", "cap",
              "box_front", "box_back", "box_bottom", "batch_code_bottle", "batch_code_packaging"]
for i, cat in enumerate(categories):
    photo = make_photo(i + 1)
    data = {"category": cat, "file": (photo, f"{cat}.jpg")}
    if "batch_code" in cat:
        data["seller_entered_code"] = "ABC123"
    resp = seller.post(f"/listings/{listing_id}/photos", data=data,
                        content_type="multipart/form-data", headers={"X-CSRF-Token": csrf(seller)})
    check(f"upload photo {cat}", resp, 201)

r = post_json(seller, f"/listings/{listing_id}/submit-for-review", {})
check("submit for review", r, 200)
print("verification result:", r.get_json())

check("web /verkopen/<id> manage after submit", seller.get(f"/verkopen/{listing_id}"))

conn = get_db()
listing_row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
conn.close()
print("listing status:", listing_row["status"], listing_row["verification_status"])

# --- Public browse / detail (only meaningful once active) ---
check("web / anon after listing", anon.get("/"))
if listing_row["status"] == "active":
    check("web /parfum/<id> anon", anon.get(f"/parfum/{listing_id}"))
else:
    print("listing not active yet (in manual review) -- will check detail as owner instead")
    check("web /parfum/<id> owner", seller.get(f"/parfum/{listing_id}"))

# If it went to manual review, push it through admin approval so we can test the buy flow too.
if listing_row["verification_status"] == "manual_review":
    r = check("admin queue", admin.get("/beheer"))
    r = check("admin review case", admin.get(f"/beheer/advertenties/{listing_id}"))
    rr = post_json(admin, f"/admin/listings/{listing_id}/review",
                    {"action": "approve", "notes": "Handmatig goedgekeurd voor e2e test."})
    print("approve attempt:", rr.status_code, rr.get_json())
    if rr.status_code != 200:
        rr = post_json(admin, f"/admin/listings/{listing_id}/review",
                        {"action": "request_more_evidence", "notes": "Nog wat extra duidelijkheid nodig."})
        print("request_more_evidence:", rr.status_code, rr.get_json())
        check("web manage after request_more_evidence", seller.get(f"/verkopen/{listing_id}"))
        # re-approve after "more evidence" -- in this dev flow we just re-submit
        rr2 = post_json(seller, f"/listings/{listing_id}/submit-for-review", {})
        print("resubmit:", rr2.status_code, rr2.get_json())
        rr = post_json(admin, f"/admin/listings/{listing_id}/review",
                        {"action": "approve", "notes": "Handmatig goedgekeurd voor e2e test (2e poging)."})
        print("approve 2nd attempt:", rr.status_code, rr.get_json())

conn = get_db()
listing_row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
conn.close()
print("final listing status:", listing_row["status"], listing_row["verification_status"])

check("web /parfum/<id> anon final", anon.get(f"/parfum/{listing_id}"))
check("web / anon final listing shows", anon.get("/"))

# --- Checkout flow (buyer) ---
check("web /afrekenen/<id>", buyer.get(f"/afrekenen/{listing_id}"))
r = post_json(buyer, "/orders/checkout", {"listing_id": listing_id})
check("checkout", r, 201)
order_id = r.get_json()["order"]["id"]
r = post_json(buyer, f"/orders/{order_id}/dev-pay", {})
check("dev-pay", r, 200)
print("order status after pay:", r.get_json()["order"]["status"])

check("web order detail (buyer)", buyer.get(f"/account/aankopen/{order_id}"))
check("web account orders (buyer)", buyer.get("/account/aankopen"))
check("web seller dashboard", seller.get("/verkopers/dashboard"))
check("web order detail (seller)", seller.get(f"/account/aankopen/{order_id}"))

r = post_json(seller, f"/orders/{order_id}/ship", {"carrier": "PostNL", "tracking_number": "3SXXX123"})
check("ship order", r, 200)

check("web seller dashboard shows payout prompt", seller.get("/verkopers/dashboard"))
r = post_json(seller, "/auth/dev-enable-payouts", {})
check("dev-enable-payouts", r, 200)

check("web order detail (admin) before delivered", admin.get(f"/account/aankopen/{order_id}"))
r = post_json(admin, f"/orders/{order_id}/mark-delivered", {})
check("mark-delivered", r, 200)
print("order status after delivered:", r.get_json()["order"]["status"])

check("web order detail (buyer) inspection", buyer.get(f"/account/aankopen/{order_id}"))
r = post_json(buyer, f"/orders/{order_id}/confirm", {})
check("confirm order", r, 200)
print("order status after confirm:", r.get_json()["order"]["status"])
check("web order detail (buyer) completed", buyer.get(f"/account/aankopen/{order_id}"))

# --- Dispute flow on a SECOND order (report-issue) ---
r = post_json(buyer, "/orders/checkout", {"listing_id": listing_id})
if r.status_code != 201:
    print("second checkout failed (listing likely no longer active) ->", r.get_json())
else:
    order2_id = r.get_json()["order"]["id"]
    post_json(buyer, f"/orders/{order2_id}/dev-pay", {})
    post_json(seller, f"/orders/{order2_id}/ship", {"carrier": "PostNL", "tracking_number": "3SXXX456"})
    r = post_json(buyer, f"/orders/{order2_id}/report-counterfeit", {"description": "De batchcode klopt niet met de doos."})
    check("report-counterfeit", r, 201)
    dispute_id = r.get_json()["dispute_id"]
    report_id = r.get_json()["authenticity_report_id"]
    check("web dispute detail (buyer)", buyer.get(f"/geschillen/{dispute_id}"))
    check("web dispute detail (seller)", seller.get(f"/geschillen/{dispute_id}"))
    check("web dispute detail (admin)", admin.get(f"/geschillen/{dispute_id}"))
    check("web admin report detail", admin.get(f"/beheer/meldingen/{report_id}"))

    r = post_json(buyer, f"/disputes/{dispute_id}/messages", {"body": "Kijk je er snel naar?"})
    check("dispute message", r, 201)
    r = post_json(admin, f"/disputes/{dispute_id}/admin-notes", {"note": "Lijkt op een echte fles, batchcode-typefout."})
    check("admin note", r, 201)
    r = post_json(admin, f"/disputes/{dispute_id}/resolve", {"resolution": "release_to_seller", "notes": "Geen namaak geconstateerd."})
    check("resolve dispute", r, 200)
    check("web dispute detail after resolve", buyer.get(f"/geschillen/{dispute_id}"))

# --- Admin misc pages ---
check("admin disputes list", admin.get("/beheer/geschillen"))
check("admin disputes resolved", admin.get("/beheer/geschillen?status=resolved"))
check("admin intelligence", admin.get("/beheer/intelligence"))
check("admin config", admin.get("/beheer/instellingen"))

# permission checks
check("buyer cannot access admin", buyer.get("/beheer"), 403)
check("anon redirected from account orders", anon.get("/account/aankopen"), 302)

print("\n\n=== SUMMARY ===")
if failures:
    print(f"{len(failures)} FAILURES")
    for f in failures:
        print("---", f[0], f[1])
        print(f[2])
else:
    print("ALL CHECKS PASSED")
