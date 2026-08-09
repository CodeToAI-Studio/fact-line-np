"""
images.py — permanent image guarantee for every published post.

Instagram feed posts REQUIRE an image, and the old pipeline failed permanently
when a post had none (stayed "pending" forever) or the source image URL was a
hotlink-protected 403 (Ratopati), expired 404 (BBC), or wrong aspect ratio
(IG error 36003). This module is the single choke point that guarantees a
usable, IG-compatible image for EVERY post:

  1. If the post has an image_url, download it, verify it's a real image,
     and normalize it to an IG-supported square (1080x1080).
  2. If there's no image OR the download fails, generate a branded "Fact Line
     NP" placeholder (with the post title) in the same IG-ready format.

The result is the normalized JPEG bytes, which the pipeline stores on
Post.image_data and serves publicly from /post_image/{id}.jpg (a route in
main.py). Instagram/Facebook then use that public URL — no external CDN, no
403/404, no aspect-ratio error, ever.
"""

import io
import os
import re
import sys
from urllib.request import urlopen, Request

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

# IG feed supports square (1:1) and 4:5 portrait. We use square — it's the
# safest and looks clean for news.
IG_SIZE = 1080          # 1080x1080
IG_ASPECT = (1, 1)

# A browser-ish User-Agent because several news CDNs (Ratopati etc.) reject
# urllib's default "Python-urllib" UA with 403.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

DOWNLOAD_TIMEOUT = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB cap to avoid memory blowups


# --- fonts ------------------------------------------------------------------
# The post title may be Nepali (Devanagari). Pillow needs a font that can
# render Devanagari, or the glyphs show as boxes. Nirmala UI ships with
# Windows and covers Devanagari + Latin; fall back to whatever TTF/TTC exists.
def _find_font() -> str | None:
    candidates = [
        r"C:\Windows\Fonts\Nirmala.ttc",          # Devanagari + Latin
        r"C:\Windows\Fonts\arial.ttf",            # Latin only
        r"C:\Windows\Fonts\segoeui.ttf",          # Latin only
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


_FONT_PATH = _find_font()


def _load_font(size: int):
    try:
        return ImageFont.truetype(_FONT_PATH, size) if _FONT_PATH else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


# --- image acquisition ------------------------------------------------------

def fetch_bytes(url: str) -> bytes | None:
    """Download the bytes at url with a bounded timeout + browser UA.
    Returns None on any failure (never raises to callers)."""
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return data
    except Exception:
        return None


def _is_valid_image(data: bytes) -> bool:
    """Confirm the bytes actually decode as an image (magic-byte via Pillow)."""
    if not data:
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False


def _normalize_to_square(img: Image.Image) -> Image.Image:
    """Center-crop to square and re-encode to RGB so IG accepts it (no 36003)."""
    img = img.convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IG_SIZE, IG_SIZE), Image.LANCZOS)
    return img


def make_placeholder(post_title: str, post_id: int) -> Image.Image:
    """Generate a branded Fact Line NP placeholder with the post title."""
    img = Image.new("RGB", (IG_SIZE, IG_SIZE), "#D4001A")  # brand red
    draw = ImageDraw.Draw(img)

    # Brand wordmark
    wordmark_font = _load_font(88)
    wordmark = "FACT LINE NP"
    try:
        wm_w = draw.textlength(wordmark, font=wordmark_font)
    except Exception:
        wm_w = 600
    draw.text(((IG_SIZE - wm_w) / 2, 180), wordmark, fill="white", font=wordmark_font)

    # Separator line
    draw.rectangle((200, 320, IG_SIZE - 200, 330), fill="#FFFFFF")

    # Title, word-wrapped to fit. Devanagari titles render via Nirmala.
    title_font = _load_font(56)
    max_w = IG_SIZE - 200
    words = re.split(r"\s+", (post_title or "Fact Line NP").strip())
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        try:
            tw = draw.textlength(trial, font=title_font)
        except Exception:
            tw = len(trial) * 30
        if tw <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    lines.append(cur)
    lines = lines[:6]  # cap at 6 lines; title will fit

    y = 420
    for ln in lines:
        try:
            tw = draw.textlength(ln, font=title_font)
        except Exception:
            tw = len(ln) * 30
        draw.text(((IG_SIZE - tw) / 2, y), ln, fill="white", font=title_font)
        y += 74

    # Footer credit
    foot_font = _load_font(36)
    foot = "FactLineNP.com"
    try:
        fw = draw.textlength(foot, font=foot_font)
    except Exception:
        fw = 300
    draw.text(((IG_SIZE - fw) / 2, IG_SIZE - 140), foot, fill="#FFDCDC", font=foot_font)

    return img


def acquire_post_image(image_url: str | None, post_title: str, post_id: int) -> bytes | None:
    """Guarantee a usable image for a post. Returns the normalized JPEG bytes,
    or None if generation failed (rare).

    The bytes are meant to be stored on Post.image_data and served publicly
    from /post_image/{id}.jpg so Instagram (which requires a public image_url)
    and Facebook always have a stable, reachable image.
    """
    # 1. Try the source image if present.
    if image_url:
        data = fetch_bytes(image_url)
        if data and _is_valid_image(data):
            try:
                with Image.open(io.BytesIO(data)) as img:
                    normalized = _normalize_to_square(img)
                buf = io.BytesIO()
                normalized.save(buf, "JPEG", quality=85)
                return buf.getvalue()
            except Exception:
                pass  # fall through to placeholder

    # 2. Fall back to a branded placeholder.
    try:
        ph = make_placeholder(post_title, post_id)
        buf = io.BytesIO()
        ph.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        print(f"    [images] placeholder generation failed for post {post_id}: {e}")
        return None


def public_image_url(post_id: int, base: str | None = None) -> str:
    """The public URL for a post's rehosted image, served from /post_image/.

    IG/FB need an absolute URL. base is the site's public origin (e.g.
    https://web-production-a8dc3.up.railway.app); falls back to a relative
    path when absent (fine for the website's own <img> tags)."""
    if base:
        return f"{base.rstrip('/')}/post_image/{post_id}.jpg"
    return f"/post_image/{post_id}.jpg"
