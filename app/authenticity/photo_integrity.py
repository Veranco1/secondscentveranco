"""
Photo integrity analysis — real, explainable, non-ML techniques run on
every uploaded evidence photo. See docs/AUTHENTICITY_ARCHITECTURE.md § E
for why these specific techniques and not a black-box "AI verified this
photo" claim: everything here is a well-understood, reproducible
computation (a cryptographic hash, a perceptual hash, a Laplacian
variance, a JPEG re-compression diff) whose limitations we can and do
state honestly, rather than an opaque model whose accuracy we cannot
verify in this environment.

Nothing in this module ever concludes "authentic" or "fake" — it only
produces signals that app/authenticity/risk_engine.py combines with
everything else it knows about the listing.
"""
import hashlib
import io

import numpy as np
from PIL import Image, ExifTags

# Common phone/desktop screenshot resolutions — used only as one weak,
# corroborating input to a "possibly a screenshot" signal, NEVER alone.
_COMMON_SCREENSHOT_RESOLUTIONS = {
    (1170, 2532), (1179, 2556), (1080, 1920), (1920, 1080), (1440, 3200),
    (750, 1334), (828, 1792), (1284, 2778), (2560, 1440), (1366, 768),
    (1920, 1200), (2732, 2048),
}

BLUR_THRESHOLD = 60.0          # Laplacian variance below this = flagged as too blurry
MANIPULATION_THRESHOLD = 18.0  # mean ELA diff above this = weak manipulation signal


class InvalidImageError(Exception):
    pass


def _load_image(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        return img
    except Exception as exc:
        raise InvalidImageError(f"Kon afbeelding niet lezen: {exc}") from exc


def sha256_of(image_bytes):
    return hashlib.sha256(image_bytes).hexdigest()


def dhash(image_bytes, hash_size=8):
    """
    Difference hash: resize to (hash_size+1, hash_size) grayscale, compare
    each pixel to its right neighbour -> hash_size*hash_size bits. Robust
    to re-compression and minor crops/resizes (which is exactly what makes
    it useful for catching a reused photo that's been re-saved), but two
    genuinely different photos of the same kind of bottle can still land
    close together — this is why photo_reuse signals in the risk engine
    only fire on a close match against a DIFFERENT seller's own upload,
    not on "looks like a perfume bottle" in general.
    """
    img = _load_image(image_bytes).convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.LANCZOS
    )
    pixels = np.asarray(img, dtype=np.int16)
    diff = pixels[:, 1:] > pixels[:, :-1]
    bits = diff.flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return format(value, f"0{hash_size * hash_size // 4}x")


def hamming_distance(hex_a, hex_b):
    if not hex_a or not hex_b:
        return None
    int_a = int(hex_a, 16)
    int_b = int(hex_b, 16)
    return bin(int_a ^ int_b).count("1")


# Safe EXIF subset: never extract/store precise GPS coordinates (privacy —
# see docs/AUTHENTICITY_ARCHITECTURE.md § I). We only record whether GPS
# data was present at all, plus a few low-sensitivity technical fields
# that are genuinely useful as weak corroborating signals.
_SAFE_EXIF_TAGS = {"Make", "Model", "DateTimeOriginal", "Software"}


def extract_exif(image_bytes):
    img = _load_image(image_bytes)
    raw = img.getexif()
    if not raw:
        return {"present": False}

    safe = {"present": True, "has_gps": False}
    for tag_id, value in raw.items():
        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if tag_name == "GPSInfo":
            safe["has_gps"] = True
            continue
        if tag_name in _SAFE_EXIF_TAGS:
            try:
                safe[tag_name] = str(value)[:200]
            except Exception:
                pass
    return safe


def blur_score(image_bytes):
    """
    Laplacian variance — a standard, well-understood sharpness measure:
    a blurry image has less high-frequency detail, so the variance of its
    Laplacian (edge response) is lower. Implemented with a straightforward
    convolution so this module doesn't require OpenCV to be present.
    """
    img = _load_image(image_bytes).convert("L")
    arr = np.asarray(img, dtype=np.float64)
    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)
    h, w = arr.shape
    if h < 3 or w < 3:
        return 0.0
    padded = np.pad(arr, 1, mode="edge")
    conv = np.zeros_like(arr)
    for dy in range(3):
        for dx in range(3):
            weight = kernel[dy, dx]
            if weight == 0:
                continue
            conv += weight * padded[dy:dy + h, dx:dx + w]
    return float(conv.var())


def manipulation_score(image_bytes, quality=90):
    """
    A simple Error Level Analysis (ELA): re-save the image at a fixed
    JPEG quality and measure how much each region's error differs from
    the rest of the image. Regions pasted in from a different source (or
    re-compressed at a different quality) tend to show a different error
    level than the surrounding, untouched image.

    Deliberately named "weak signal" throughout this codebase: ELA has a
    well-known high false-positive rate (legitimate crops, text overlays,
    and heavy compression all produce similar artifacts), so
    risk_engine.py gives it a low weight and it is NEVER, on its own,
    grounds for rejection — see docs/AUTHENTICITY_ARCHITECTURE.md § E.
    """
    img = _load_image(image_bytes).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    buf.seek(0)
    resaved = Image.open(buf)
    resaved.load()

    orig_arr = np.asarray(img, dtype=np.int16)
    resaved_arr = np.asarray(resaved, dtype=np.int16)
    diff = np.abs(orig_arr - resaved_arr)

    # Compare the variance of per-pixel error across quadrants: a
    # genuinely uniform photo has similar error everywhere; a spliced
    # region stands out as a quadrant with an outlier mean error.
    h, w, _ = diff.shape
    if h < 4 or w < 4:
        return float(diff.mean())
    mid_h, mid_w = h // 2, w // 2
    quadrants = [
        diff[:mid_h, :mid_w], diff[:mid_h, mid_w:],
        diff[mid_h:, :mid_w], diff[mid_h:, mid_w:],
    ]
    means = [float(q.mean()) for q in quadrants]
    return float(max(means) - min(means))


def looks_like_screenshot(image_bytes, exif):
    img = _load_image(image_bytes)
    dims = img.size
    return (not exif.get("present")) and (
        dims in _COMMON_SCREENSHOT_RESOLUTIONS or tuple(reversed(dims)) in _COMMON_SCREENSHOT_RESOLUTIONS
    )


def analyze(image_bytes):
    """
    Runs every check above once and returns one dict — this is what gets
    stored (as JSON) in listing_photos.integrity_flags and is what
    app/authenticity/risk_engine.py reads to decide whether to raise
    low_quality_evidence_photo / possible_manipulation_signal signals.
    Raises InvalidImageError for anything that isn't a readable image —
    callers should turn that into a 400, not a 500.
    """
    img = _load_image(image_bytes)
    width, height = img.size
    exif = extract_exif(image_bytes)
    blur = blur_score(image_bytes)
    manipulation = manipulation_score(image_bytes)

    return {
        "sha256": sha256_of(image_bytes),
        "phash": dhash(image_bytes),
        "width": width,
        "height": height,
        "exif": exif,
        "blur_score": round(blur, 2),
        "is_blurry": blur < BLUR_THRESHOLD,
        "manipulation_score": round(manipulation, 2),
        "possible_manipulation": manipulation > MANIPULATION_THRESHOLD,
        "possible_screenshot": looks_like_screenshot(image_bytes, exif),
    }
