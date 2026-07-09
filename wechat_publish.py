#!/usr/bin/env python3
"""
WeChat Official Account article publisher.
Uploads images, creates draft, and optionally publishes.

Requires: APP_ID and APP_SECRET environment variables.

Usage:
    python3 wechat_publish.py --article article.md --images-dir ./images [--publish]
"""
import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"

# Credentials — loaded from .env in same directory, then env vars
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

WECHAT_APP_ID = os.environ.get("WECHAT_APP_ID", "")
WECHAT_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")


# Pre-uploaded permanent cover image media_ids (material/add_material)
# Generated once, reused for all articles.
# - cover-report.png: "报告" 紫色封面（用于报告解读类文章）
# - cover-news.png: "资讯" 紫色封面（用于资讯/Blog类文章）
# - cover-depth.png: "深度" 紫色封面（用于深度分析/周报类文章）
PERMANENT_COVERS = {
    # 2.35:1 大图（文章顶部展示）
    "report": "9NLTUyJrY8wPG6UQHaGXlZpN_bNYqC4Luprvfx2yeOROIPGBgqv-zvfESamPCmHc",
    "news": "9NLTUyJrY8wPG6UQHaGXlXtOO1DD9ccc-Bc99rnbuRymMif4oXnNMearG_gizAW7",
    "depth": "9NLTUyJrY8wPG6UQHaGXlfIl3GH8_j7dT_8VbxHWebOOLEh2PdpF9dOaKjhH0LRJ",
    # 1:1 封面缩略图（公众号列表页展示）
    "report-1x1": "9NLTUyJrY8wPG6UQHaGXlW1F5PiJf9GldKSYvgJyBjWvbI2CFd-W5gPs7isUU1no",
    "news-1x1": "9NLTUyJrY8wPG6UQHaGXlYd6Rjl3a07Lf4qrow1iqDz7nn7yRSAR9v8pcyuhvqL9",
    "depth-1x1": "9NLTUyJrY8wPG6UQHaGXlaN-7AHv1LVXYOWf3xEc8ADvfNSN3LQs5fLrcElyQppp",
}

PURPLE = "#7c3aed"


class WeChatPublisher:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._token_expires = 0

    def _get_access_token(self) -> str:
        """Get or refresh access token."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        url = f"{WECHAT_API_BASE}/token?grant_type=client_credential&appid={self.app_id}&secret={self.app_secret}"
        resp = self._request("GET", url)
        self._token = resp["access_token"]
        self._token_expires = time.time() + resp["expires_in"]
        print(f"🔑 Access token obtained (expires in {resp['expires_in']}s)")
        return self._token

    def _request(self, method: str, url: str, data: dict = None, files: dict = None) -> dict:
        """Make HTTP request and parse JSON response."""
        if files:
            # Multipart upload
            import io
            boundary = "----WebKitFormBoundary" + hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
            body = io.BytesIO()
            for field_name, (filename, filedata, content_type) in files.items():
                body.write(f"--{boundary}\r\n".encode())
                body.write(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode())
                body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
                body.write(filedata)
                body.write(b"\r\n")
            body.write(f"--{boundary}--\r\n".encode())
            data_bytes = body.getvalue()
            req = Request(url, data=data_bytes, method=method)
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            req.add_header("Content-Length", str(len(data_bytes)))
        elif data:
            req = Request(url, data=json.dumps(data, ensure_ascii=False).encode("utf-8"), method=method)
            req.add_header("Content-Type", "application/json; charset=utf-8")
        else:
            req = Request(url, method=method)

        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise RuntimeError(f"WeChat HTTP {e.code}: {body}")
        except (URLError, socket.timeout, TimeoutError) as e:
            raise RuntimeError(f"WeChat network error: {e}")
        except Exception as e:
            raise RuntimeError(f"WeChat request failed: {e}")

        if "errcode" in result and result["errcode"] != 0:
            raise RuntimeError(f"WeChat API error {result['errcode']}: {result.get('errmsg', '')}")
        return result

    def upload_image(self, filepath: str) -> str:
        """Upload a local image as permanent material. Returns media_id."""
        token = self._get_access_token()
        url = f"{WECHAT_API_BASE}/material/add_material?access_token={token}&type=image"

        with open(filepath, "rb") as f:
            filedata = f.read()
        filename = os.path.basename(filepath)
        content_type = "image/png" if filepath.endswith(".png") else "image/jpeg"

        result = self._request("POST", url, files={
            "media": (filename, filedata, content_type),
        })
        media_id = result.get("media_id", "")
        url_returned = result.get("url", "")
        print(f"🖼️ Image uploaded: {filename} → media_id: {media_id}")
        return media_id

    def upload_image_as_url(self, filepath: str) -> str:
        """Upload image and return WeChat CDN URL (for inline use in article body)."""
        token = self._get_access_token()
        url = f"{WECHAT_API_BASE}/media/uploadimg?access_token={token}"

        with open(filepath, "rb") as f:
            filedata = f.read()
        filename = os.path.basename(filepath)
        content_type = "image/png" if filepath.endswith(".png") else "image/jpeg"

        result = self._request("POST", url, files={
            "media": (filename, filedata, content_type),
        })
        img_url = result.get("url", "")
        print(f"🖼️ Image URL uploaded: {filename} → {img_url[:60]}...")
        return img_url

    def upload_article_images_as_urls(self, images_dir: str, max_workers: int = 4) -> dict:
        """Upload all inline images concurrently. Returns {filename: cdn_url}.

        Fault tolerance: a single image failing (network error, size limit,
        45009/40007, etc.) is skipped with a warning instead of aborting the
        whole publish. Cover files (cover-1x1 / cover-wide) are excluded — they
        are only used as the draft thumbnail, never as inline body images.
        """
        if not os.path.isdir(images_dir):
            print(f"⚠️ Images directory not found: {images_dir}")
            return {}

        # Files that must NOT be inlined into the article body.
        COVER_NAMES = {"cover-1x1.jpg", "cover-1x1.png", "cover-wide.jpg", "cover-wide.png"}

        candidates = []
        for fname in sorted(os.listdir(images_dir)):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")) and fname not in COVER_NAMES:
                candidates.append(fname)

        if not candidates:
            print("  ℹ️ No inline images to upload")
            return {}

        def _upload_one(fname: str):
            fpath = os.path.join(images_dir, fname)
            try:
                return fname, self.upload_image_as_url(fpath)
            except Exception as e:
                print(f"  ⚠️ Skipping image {fname} (upload failed): {e}")
                return fname, None

        mapping = {}
        # Concurrent upload — each call has its own 30s urlopen timeout inside
        # _request, so a slow/blocked image can't stall the whole batch.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_upload_one, f): f for f in candidates}
            for fut in as_completed(futures):
                fname, cdn_url = fut.result()
                if cdn_url:
                    mapping[fname] = cdn_url
        return mapping

    def upload_article_images(self, images_dir: str) -> dict:
        """Upload all images in a directory. Returns {filename: media_id} mapping."""
        if not os.path.isdir(images_dir):
            print(f"⚠️ Images directory not found: {images_dir}")
            return {}

        mapping = {}
        for fname in sorted(os.listdir(images_dir)):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                fpath = os.path.join(images_dir, fname)
                media_id = self.upload_image(fpath)
                mapping[fname] = media_id
        return mapping

    def parse_markdown_article(self, article_path: str, digest: str = "") -> dict:
        """Parse Markdown file into WeChat article format.

        Digests are resolved in priority order:
          1. Explicit --digest argument
          2. meta.json in the same directory
        """
        md_path = Path(article_path)
        with md_path.open("r", encoding="utf-8") as f:
            content = f.read()

        # Try to load digest from meta.json if --digest not provided
        if not digest:
            meta_path = md_path.parent / "meta.json"
            if meta_path.exists():
                try:
                    import json
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    digest = meta.get("digest", "")
                except (json.JSONDecodeError, IOError):
                    pass

        # Extract title (first # heading)
        lines = content.split("\n")
        title = ""
        body_lines = []
        for line in lines:
            if line.startswith("# ") and not title:
                title = line[2:].strip()
            else:
                body_lines.append(line)

        # Convert Markdown body to WeChat-compatible format
        body = self._markdown_to_wechat_html("\n".join(body_lines))

        # Prepend subscription banner to body
        banner = '''<p style="margin: 0 0 20px 0; padding: 12px 16px; background-color: #f5f0ff; border-radius: 8px; font-size: 15px; text-align: center; color: #5b21b6; line-height: 1.8;">关注并加入星标，每天 7<span style="color: #333;">:</span><span style="color: #333;">33</span> 准时送达一手洞察 🌟</p>'''
        body = banner + body

        result = {"title": title or "未命名文章", "content": body}
        if digest:
            result["digest"] = digest
        return result

    # ── Inline Markdown processing ────────────────────────────────────

    def _process_inline(self, text: str) -> str:
        """Process inline formatting for WeChat.
        WeChat editor only supports <strong>, <em>, <span style="...">.
        Stick to <strong> which is reliably rendered.
        """
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
        text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        # Links: [text](url) → WeChat supports <a> in content
        text = re.sub(r'\[([^\]]+)\]\(([^)]*)\)',
                      lambda m: f'{m.group(1)}（<a href="{m.group(2)}" target="_blank">链接</a>）', text)
        return text

    # ── Markdown → WeChat content conversion ────────────────────────

    def _markdown_to_wechat_html(self, md: str) -> str:
        """Convert Markdown to WeChat-compatible content.

        WeChat draft API content field accepts HTML, but the internal editor
        strips or re-writes many tags. Key rules observed in practice:
        - <h1>-<h6> stripped to plain text → render headings as <p> + <strong> + font-size
        - <ul>/<ol><li> stripped to plain text → render list items as <p> with bullet prefix
        - <strong> works for bold
        - <em> works for italic
        - <span> with style supported for limited inline styles (font-weight, font-size, color)
        - <blockquote> may work but unreliable → render with inline style
        - <img> works! src must be WeChat CDN URL (from uploadimg API)
        - <a href="..."> works for links
        - Double quotes in attributes can cause 'invalid media id' → use single quotes
        """
        sections = []
        i = 0
        lines = md.split("\n")

        # Heading size mapping
        HEADING_SIZES = {1: "22px", 2: "20px", 3: "18px", 4: "17px"}

        while i < len(lines):
            stripped = lines[i].strip()

            # Skip empty lines
            if not stripped:
                i += 1
                continue

            # ── Table ──
            if stripped.startswith('|') and stripped.endswith('|'):
                # Collect consecutive table rows
                table_lines = []
                while i < len(lines) and lines[i].strip().startswith('|') and lines[i].strip().endswith('|'):
                    table_lines.append(lines[i].strip())
                    i += 1

                if len(table_lines) >= 2:  # header + separator at minimum
                    rows = []
                    for tl in table_lines:
                        cells = [c.strip() for c in tl[1:-1].split('|')]
                        rows.append(cells)

                    # Row 0: header, Row 1: alignment, Row 2+: data
                    if len(rows) >= 2 and all(re.match(r'^[-: ]+$', c) for c in rows[1]):
                        header_cells = rows[0]
                        data_rows = rows[2:]

                        html_parts = []
                        html_parts.append('<table style="border-collapse: collapse; width: 100%; font-size: 14px; line-height: 1.6; margin: 16px 0; border: 1px solid #d5cce8;">')

                        # Header row — per-cell styling for WeChat compatibility
                        html_parts.append('<tr>')
                        for cell in header_cells:
                            html_parts.append('<td style="background-color: ' + PURPLE + '; padding: 10px 14px; font-weight: bold; border: 1px solid #5b21b6; color: #ffffff;">' + self._process_inline(cell) + '</td>')
                        html_parts.append('</tr>')

                        # Data rows — per-row styling, no tbody
                        for row in data_rows:
                            bg = '#f9f6fe' if (data_rows.index(row) % 2 == 0) else '#ffffff'
                            html_parts.append(f'<tr>')
                            for cell in row:
                                html_parts.append('<td style="padding: 10px 14px; border: 1px solid #e5e0f0; color: #333333; background-color: ' + bg + ';">' + self._process_inline(cell) + '</td>')
                            html_parts.append('</tr>')

                        html_parts.append('</table>')
                        sections.append(''.join(html_parts))
                        continue
                    else:
                        # Not a valid table, unwind and treat as paragraph
                        i -= len(table_lines)

            # ── Image ──
            img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', stripped)
            if img_match:
                sections.append(f'<p style="margin: 24px 0;"><img src="{img_match.group(2)}" alt="{img_match.group(1)}" style="max-width: 100%;"/></p>')
                i += 1
                continue

            # ── Heading ──
            h_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
            if h_match:
                level = len(h_match.group(1))
                text = h_match.group(2)
                processed = self._process_inline(text)
                size = HEADING_SIZES.get(level, "16px")
                if level == 1:
                    color = PURPLE
                    margin_top = "32px"
                    margin_bottom = "16px"
                elif level == 2:
                    color = PURPLE
                    margin_top = "28px"
                    margin_bottom = "12px"
                elif level == 3:
                    color = PURPLE
                    margin_top = "24px"
                    margin_bottom = "10px"
                else:
                    color = PURPLE
                    margin_top = "20px"
                    margin_bottom = "10px"
                sections.append(f'<p style="font-weight: bold; font-size: {size}; color: {color}; margin-top: {margin_top}; margin-bottom: {margin_bottom}; line-height: 1.5;">{processed}</p>')
                i += 1
                continue

            # ── Horizontal rule ──
            if re.match(r'^[-*_]{3,}\s*$', stripped):
                sections.append('<p style="border-bottom: 1px solid #ddd; margin: 28px 0;"></p>')
                i += 1
                continue

            # ── Ordered list ──
            ol_match = re.match(r'^(\d+)[.、]\s+(.+)$', stripped)
            if ol_match:
                num = ol_match.group(1)
                text = ol_match.group(2)
                processed = self._process_inline(text)
                sections.append(f'<p style="text-indent: 0em; line-height: 1.6; margin-top: 0; margin-bottom: 4px;">{num}. {processed}</p>')
                i += 1
                continue

            # ── Unordered list ──
            if stripped.startswith("- ") or stripped.startswith("* "):
                text = stripped[2:]
                processed = self._process_inline(text)
                sections.append(f'<p style="text-indent: 0em; line-height: 1.6; margin-top: 0; margin-bottom: 4px;">&#8226; {processed}</p>')
                i += 1
                continue

            # ── Blockquote ──
            if stripped.startswith("> "):
                text = stripped[2:]
                processed = self._process_inline(text)
                sections.append(f'<p style="border-left: 3px solid {PURPLE}; '
                               f'padding-left: 14px; color: #555; line-height: 1.6; margin: 16px 0;">{processed}</p>')
                i += 1
                continue

            # ── Regular paragraph ──
            para_lines = []
            while i < len(lines) and lines[i].strip():
                check = lines[i].strip()
                if re.match(r'^(#{1,6})\s', check):
                    break
                if re.match(r'^\d+[.、]\s', check):
                    break
                if check.startswith("- ") or check.startswith("* "):
                    break
                if check.startswith("> "):
                    break
                if re.match(r'^!\[.*?\]\(.*?\)$', check):
                    break
                if re.match(r'^[-*_]{3,}\s*$', check):
                    break
                para_lines.append(self._process_inline(check))
                i += 1

            if para_lines:
                processed = " ".join(para_lines)
                sections.append(f"<p style=\"line-height: 1.6; margin-top: 0; margin-bottom: 12px;\">{processed}</p>")

        return "\n".join(sections)

    def _upload_thumb(self, images_dir: str) -> str:
        """Upload cover image (thumb_media_id). Prefers cover.*, then first image."""
        files = []
        for fname in os.listdir(images_dir):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                files.append(fname)
        # Prioritize cover.* files
        cover = [f for f in files if f.lower().startswith("cover")]
        if cover:
            return self.upload_image(os.path.join(images_dir, cover[0]))
        elif files:
            return self.upload_image(os.path.join(images_dir, sorted(files)[0]))
        return ""

    AUTHOR_NAME = "徐厚畅"

    def create_draft(self, article_data: dict, thumb_media_id: str = "") -> str:
        """Create a WeChat draft. Returns media_id."""
        token = self._get_access_token()
        url = f"{WECHAT_API_BASE}/draft/add?access_token={token}"

        body = {
            "articles": [{
                "title": article_data["title"],
                "author": self.AUTHOR_NAME,
                "digest": article_data.get("digest", ""),
                "content": article_data["content"],
                "thumb_media_id": thumb_media_id,
                "need_open_comment": 1,
                "only_fans_can_comment": 1,
            }]
        }

        result = self._request("POST", url, data=body)
        media_id = result.get("media_id", "")
        print(f"📝 Draft created: {article_data['title']} → media_id: {media_id}")
        return media_id

    def publish_draft(self, media_id: str) -> str:
        """Publish a draft. Returns publish_id."""
        token = self._get_access_token()
        url = f"{WECHAT_API_BASE}/freepublish/submit?access_token={token}"

        body = {"media_id": media_id}
        result = self._request("POST", url, data=body)
        publish_id = result.get("publish_id", "")
        print(f"🚀 Published! publish_id: {publish_id}")
        return publish_id


def update_url_status(url: str, new_status: str = "used"):
    """Update a URL's status in all_urls.tsv."""
    tsv_path = Path(__file__).parent.parent / "reports" / "_index" / "all_urls.tsv"
    if not tsv_path.exists():
        return
    lines = tsv_path.read_text(encoding="utf-8").splitlines()
    changed = 0
    for i in range(len(lines)):
        if not lines[i] or lines[i].startswith("#"):
            continue
        parts = lines[i].split("\t")
        if len(parts) >= 2 and parts[0] == url and parts[1] != new_status:
            date = parts[2] if len(parts) > 2 else ""
            lines[i] = f"{url}\t{new_status}\t{date}"
            changed += 1
    if changed:
        tsv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  ✅ Marked {changed} entry/entries as '{new_status}' for: {url}")


def mark_article_sources(article_path: str):
    """Scan article for URLs and mark them as 'used' in the registry."""
    if not os.path.exists(article_path):
        return
    text = Path(article_path).read_text(encoding="utf-8")
    found_urls = re.findall(r'https?://[^\s<\)\]\']+', text)
    for u in found_urls:
        u = u.rstrip(')').rstrip('.').rstrip('/')
        if u.startswith('http'):
            update_url_status(u, 'used')

def main():
    parser = argparse.ArgumentParser(description="Publish article to WeChat Official Account")
    parser.add_argument("--article", "-a", required=True, help="Markdown article file path")
    parser.add_argument("--images-dir", "-i", default="./images", help="Images directory")
    parser.add_argument("--digest", default="", help="Article abstract/summary for digest field")
    parser.add_argument("--publish", action="store_true", help="Actually publish (not just draft)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without creating draft")
    parser.add_argument("--no-cover", action="store_true", help="Skip dynamic cover (use permanent cover only, no inline images)")
    parser.add_argument("--app-id", help="WeChat APP_ID (overrides env var)")
    parser.add_argument("--app-secret", help="WeChat APP_SECRET (overrides env var)")
    args = parser.parse_args()

    # Credentials: use constants defined at top of file
    if not WECHAT_APP_ID or not WECHAT_APP_SECRET:
        print("⚠️  WECHAT_APP_ID / WECHAT_APP_SECRET not configured. Cannot publish.")
        print("    Set them in scripts/.env or as environment variables.")
        sys.exit(1)

    publisher = WeChatPublisher(WECHAT_APP_ID, WECHAT_APP_SECRET)

    # Step 1: Parse article
    print("📖 Parsing article...")
    article = publisher.parse_markdown_article(args.article, digest=args.digest)
    print(f"   Title: {article['title']}")
    print(f"   Length: {len(article['content'])} chars")

    # Step 2: Generate dynamic cover image (skip if --no-cover)
    if not args.no_cover:
        print("🎨 Generating cover image from Pexels...")
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from generate_cover import generate_cover_from_file as gen_cover
            cover_ok = gen_cover(args.article, args.images_dir)
            if cover_ok and len(cover_ok) >= 2 and cover_ok[0] and cover_ok[1]:
                print(f"  ✅ Cover images generated in {args.images_dir}")
            else:
                print(f"  ⚠️ Cover generation partial, falling back to permanent cover")
        except ImportError as e:
            print(f"  ⚠️ generate_cover.py not available ({e}), using fallback")
        except Exception as e:
            print(f"  ⚠️ Cover generation error: {e}")
    else:
        print("🎨 Skipping cover generation (--no-cover)")

    # Step 3: Upload images for inline use (cgi-bin/media/uploadimg → CDN URL)
    # Skip if --no-cover (no inline images either)
    url_map = {}
    if not args.no_cover:
        print("🖼️ Uploading images for article body...")
        try:
            url_map = publisher.upload_article_images_as_urls(args.images_dir)
        except Exception as e:
            print(f"  ⚠️ Inline image upload failed (continuing without them): {e}")
            url_map = {}

    # Step 4: Replace local image paths with WeChat CDN URLs
    replaced = 0
    for fname, cdn_url in url_map.items():
        for prefix in ["/images/", "images/", "./images/", "./images_root/", "./images/nathan-real/", "images/nathan-real/", "nathan-real/", ""]:
            src_path = prefix + fname
            new_content = article["content"].replace(f'src="{src_path}"', f'src="{cdn_url}"')
            if new_content != article["content"]:
                replaced += 1
            article["content"] = new_content
    if url_map and replaced == 0:
        print("  ⚠️ No local image src matched the uploaded files; check embed/naming")

    # Step 5: Upload cover image as permanent material -> use as thumb_media_id
    # Prefer dynamically generated cover; fall back to first body image
    thumb_media_id = ""
    if not args.dry_run:
        # Try cover-1x1.jpg first (dynamically generated square crop)
        for candidate in ["cover-1x1.jpg", "cover-1x1.png", "cover-wide.jpg", "cover-wide.png"]:
            candidate_path = os.path.join(args.images_dir, candidate)
            if os.path.exists(candidate_path):
                try:
                    thumb_media_id = publisher.upload_image(candidate_path)
                    print(f"🖼️ Uploaded dynamic cover: {candidate} → {thumb_media_id[:40]}...")
                    break
                except Exception as e:
                    print(f"  ⚠️ Failed to upload {candidate}: {e}")

    # Fallback: use permanent cover if dynamic generation failed
    if not thumb_media_id:
        title_lower = article['title'].lower()
        content_preview = article['content'][:300].lower()
        combined = title_lower + ' ' + content_preview
        if any(kw in combined for kw in ['报告', 'report', '白皮书', '白皮書', '深度解读', '解读', 'analysis', 'index', '趋势']):
            cover_type = "report"
        else:
            cover_type = "news"
        thumb_media_id = PERMANENT_COVERS.get(f"{cover_type}-1x1", PERMANENT_COVERS.get(cover_type, PERMANENT_COVERS["news"]))
        print(f"🖼️ Using permanent cover ({cover_type}): {thumb_media_id[:40]}...")

    # Step 6: Create draft
    media_id = publisher.create_draft(article, thumb_media_id)
    print(f"✅ Draft created: {media_id}")

    # Step 6a: Mark sources as used in URL registry
    mark_article_sources(args.article)

    # Step 7: Optionally publish
    if args.publish:
        pub_id = publisher.publish_draft(media_id)
        print(f"✅ Published! ID: {pub_id}")
        print("🔗 发布后可在公众号后台查看 https://mp.weixin.qq.com/")
    else:
        print("⏸️  Draft saved (not published). Use --publish to publish.")


if __name__ == "__main__":
    main()
