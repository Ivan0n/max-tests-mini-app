"""Task images: S3-compatible storage (SeaweedFS in Compose), image checks and signed URLs.

Uploads are decoded and re-encoded to WebP with Pillow, so only real pictures are stored and
their metadata (EXIF with GPS, camera, author) is dropped. Images are served by the backend
at /api/images/<id>?exp=<unix time>&sig=<HMAC>: an <img> tag cannot send the Bearer token,
so the API hands out short-lived signed links instead, and S3 itself stays inside Docker.
"""
import base64
import hashlib
import hmac
import io
import os
import re
import time
import warnings

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SIDE = 2048
MAX_PIXELS = 40_000_000
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP"}
CONTENT_TYPE = "image/webp"
IMAGE_ID_RE = re.compile(r"[0-9a-f]{32}")
# Our own image URL in any form we ever produce: relative or absolute, signed or not.
INTERNAL_SRC_RE = re.compile(r"^(?:https?://[^/?#]+)?/api/images/([0-9a-f]{32})(?:\?[^#]*)?$")
DAY = 86400


class InvalidImage(ValueError):
    pass


def configured() -> bool:
    return bool(os.getenv("S3_ENDPOINT") and os.getenv("S3_ACCESS_KEY") and os.getenv("S3_SECRET_KEY"))


def signing_key() -> bytes:
    """IMAGE_SIGNING_KEY, or a key derived from the bot token (a secret the operator already has),
    or from the S3 secret: stable across restarts and the same in every worker."""
    explicit = os.getenv("IMAGE_SIGNING_KEY", "").strip()
    if explicit:
        return explicit.encode()
    secret = os.getenv("MAX_BOT_TOKEN", "").strip() or os.getenv("S3_SECRET_KEY", "")
    return hmac.new(secret.encode(), b"max-tests:image-link-signing", hashlib.sha256).digest()


def bucket() -> str:
    return os.getenv("S3_BUCKET", "test-images")


_client = None


def client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name=os.getenv("S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=3, read_timeout=15, retries={"max_attempts": 3}),
        )
    return _client


def ensure_bucket(attempts: int = 45, delay: float = 2.0) -> None:
    """Create the bucket if needed; waits for the storage to come up (db-init runs once at start).

    Right after the first start the storage can report healthy while it is still setting itself up,
    so any storage error, also on create_bucket, is retried instead of failing the whole start.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    last = None
    for _ in range(attempts):
        try:
            try:
                client().head_bucket(Bucket=bucket())
                return
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
                    raise
            try:
                client().create_bucket(Bucket=bucket())
            except ClientError as error:
                # Created by a parallel start in the meantime: that is fine.
                if error.response.get("Error", {}).get("Code") not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise
            return
        except (BotoCoreError, ClientError) as error:
            last = error
        time.sleep(delay)
    raise RuntimeError(f"S3 storage is not available: {last}")


def object_key(image_id: str) -> str:
    return f"images/{image_id}.webp"


def put_image(image_id: str, data: bytes) -> None:
    client().put_object(Bucket=bucket(), Key=object_key(image_id), Body=data, ContentType=CONTENT_TYPE)


def get_image(key: str) -> bytes:
    return client().get_object(Bucket=bucket(), Key=key)["Body"].read()


def prepare_image(raw: bytes) -> tuple[bytes, int, int]:
    """Decode an upload and re-encode it as WebP: (data, width, height). Raises InvalidImage."""
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise InvalidImage("Image is empty or too large")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ACCEPTED_FORMATS:
                    raise InvalidImage("Only PNG, JPEG and WebP images are accepted")
                image.seek(0)  # animated images keep their first frame
                image.load()
                picture = ImageOps.exif_transpose(image)
                has_alpha = picture.mode in ("RGBA", "LA", "PA") or (
                    picture.mode == "P" and "transparency" in picture.info)
                picture = picture.convert("RGBA" if has_alpha else "RGB")
                picture.thumbnail((MAX_SIDE, MAX_SIDE))
                out = io.BytesIO()
                picture.save(out, "WEBP", quality=85, method=4)
                return out.getvalue(), picture.width, picture.height
        except InvalidImage:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise InvalidImage("Image is too large") from error
        except (OSError, ValueError, SyntaxError, EOFError) as error:
            raise InvalidImage("Not a valid image") from error
        finally:
            Image.MAX_IMAGE_PIXELS = previous


def _signature(image_id: str, expires: int) -> str:
    key = signing_key()
    digest = hmac.new(key, f"{image_id}:{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def signed_path(image_id: str, now: float | None = None) -> str:
    """Valid until the end of the next UTC day: the same URL all day, so browsers can cache it."""
    now = time.time() if now is None else now
    expires = (int(now) // DAY + 2) * DAY
    return f"/api/images/{image_id}?exp={expires}&sig={_signature(image_id, expires)}"


def verify(image_id: str, expires: str | None, signature: str | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if not IMAGE_ID_RE.fullmatch(image_id or "") or not expires or not signature or not expires.isdigit():
        return False
    exp = int(expires)
    if exp < now or exp > now + 3 * DAY:
        return False
    return hmac.compare_digest(signature, _signature(image_id, exp))


def internal_image_id(src) -> str | None:
    match = INTERNAL_SRC_RE.match(src) if isinstance(src, str) else None
    return match.group(1) if match else None
