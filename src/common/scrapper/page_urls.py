"""Generic page admission: content parameters remain intact, asset paths do not crawl."""
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

ASSET_SUFFIXES = frozenset(('js mjs cjs css map json xml txt csv pdf '
    'png jpg jpeg gif webp avif svg ico bmp tiff '
    'woff woff2 ttf otf eot wasm webmanifest '
    'mp3 mp4 wav ogg webm mov avi zip gz tar rar 7z').split())


def is_page_url(url):
    try:
        parsed = urlsplit(url)
        suffix = PurePosixPath(unquote(parsed.path).rstrip('/')).suffix.lower().lstrip('.')
        return parsed.scheme.lower() in ('http','https') and bool(parsed.hostname) and suffix not in ASSET_SUFFIXES
    except ValueError:
        return False
