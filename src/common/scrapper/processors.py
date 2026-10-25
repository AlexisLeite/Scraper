"""Playwright capture adapter; extraction is provided by the semantic strategy."""
from ..uri_to_html import capture_page
from .models import PageCapture

class PlaywrightUriToHtml:
    def __call__(self, uri: str) -> PageCapture:
        html, screenshot, final_uri, title = capture_page(uri)
        return PageCapture(uri, final_uri, title, html, screenshot or None, interactive=True)
