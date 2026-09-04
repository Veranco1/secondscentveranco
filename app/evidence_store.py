"""
Private evidence file storage — listing photos, counterfeit-report
photos, proof of purchase. See docs/AUTHENTICITY_ARCHITECTURE.md § I:
files live outside anything web-served directly, and are only ever
returned through an authenticated, authorization-checked endpoint
(app/listings/routes.py::get_photo_file, app/disputes/routes.py's
existing evidence handling). This module never runs from a public URL —
`file_ref` is a path on this filesystem, not a link.

PRODUCTION NOTE (stated once, honestly, rather than pretended away):
local disk is a stand-in for this sandbox. A real deployment should
replace this with private object storage (S3 + signed URLs or
equivalent) — same "swap the implementation, not the callers" pattern
already used for app/db.py and app/payments/stripe_client.py.
"""
import os
import re
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_ROOT = os.environ.get(
    "EVIDENCE_STORE_PATH", os.path.join(os.path.dirname(BASE_DIR), "data", "evidence")
)

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidPathError(Exception):
    pass


def _safe(segment):
    if not _SAFE_SEGMENT.match(segment):
        raise InvalidPathError(f"unsafe path segment: {segment!r}")
    return segment


def save(namespace, owner_id, data, extension="jpg"):
    """
    namespace: 'listing_photos' | 'dispute_evidence' | ... — keeps
    different kinds of evidence in separate subtrees.
    owner_id: the listing_id / dispute_id this file belongs to.
    Returns a file_ref (relative path) to store in the database — never
    an absolute filesystem path and never a URL.
    """
    namespace = _safe(namespace)
    owner_id = _safe(owner_id)
    extension = _safe(extension)
    directory = os.path.join(EVIDENCE_ROOT, namespace, owner_id)
    os.makedirs(directory, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{extension}"
    path = os.path.join(directory, filename)
    with open(path, "wb") as f:
        f.write(data)
    return os.path.join(namespace, owner_id, filename)


def read(file_ref):
    """
    Reads a file back by its stored file_ref. Rejects anything that
    would escape EVIDENCE_ROOT (defense in depth — file_ref values are
    always ones this module generated, never user input, but a route
    handler bug elsewhere should not turn into a path traversal).
    """
    full_path = os.path.realpath(os.path.join(EVIDENCE_ROOT, file_ref))
    root = os.path.realpath(EVIDENCE_ROOT)
    if not full_path.startswith(root + os.sep):
        raise InvalidPathError(f"file_ref escapes evidence root: {file_ref!r}")
    with open(full_path, "rb") as f:
        return f.read()
