#!/usr/bin/env python3
"""Publish a generated article as a WeChat draft."""
import json, os, sys, requests, time, re
from pathlib import Path

ENV_PATH = Path(__file__).parent / '.env'

def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().strip().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def get_token(app_id, app_secret):
    r = requests.get(
        'https://api.weixin.qq.com/cgi-bin/token',
        params={'grant_type': 'client_credential', 'appid': app_id, 'secret': app_secret}
    )
    data = r.json()
    if 'access_token' in data:
        return data['access_token']
    raise Exception(f"get_token failed: {data}")

def upload_image(token, image_path):
    """Upload image as permanent material, return media_id."""
    with open(image_path, 'rb') as f:
        r = requests.post(
            'https://api.weixin.qq.com/cgi-bin/material/add_material',
            params={'access_token': token, 'type': 'image'},
            files={'media': (os.path.basename(image_path), f, 'image/jpeg')}
        )
    data = r.json()
    if 'media_id' in data:
        return data['media_id']
    raise Exception(f"upload_image failed: {data}")

def upload_image_get_url(token, image_path):
    """Upload image, return WeChat CDN image URL (for article body images)."""
    with open(image_path, 'rb') as f:
        r = requests.post(
            'https://api.weixin.qq.com/cgi-bin/material/add_material',
            params={'access_token': token, 'type': 'image'},
            files={'media': (os.path.basename(image_path), f, 'image/jpeg')}
        )
    data = r.json()
    if 'url' in data:
        return data['url']
    raise Exception(f"upload_image_get_url failed: {data}")


def _add_header_template(raw_md: str, title: str) -> str:
    """Prepend brand template header to article content."""
    now = time.strftime('%Y-%m-%d')
    template = (
        "\n"
        "> 内容中枢 \u00b7 每日精选\n"
        f"> {now}   \u00b7  #AI\u521b\u4e1a  #\u5927\u6a21\u578b  #\u5546\u4e1a\u6d1e\u5bdf\n"
        "> ---\n"
        "\n"
    )
    return template.strip() + '\n\n' + raw_md

def _md_to_html(md_text):
    """Convert markdown to WeChat-compatible HTML with proper typography.

    WeChat article HTML only supports inline CSS and a limited subset of tags.
    Key spacing/typography rules:
      - Base font: 16px, line-height 1.8 for readability on mobile
      - H2: 18px bold, top margin 28px, bottom 12px
      - H3: 17px bold, top margin 20px, bottom 8px
      - Paragraph: 16px, line-height 1.8
      - Blockquote: 15px, left purple border (#7c3aed), light purple bg
      - Image: width 100%, border-radius 8px, margin 16px 0
      - HR: styled divider
      - UL/LI: proper list indentation
    """
    lines = md_text.strip().split('\n')
    html_parts = []
    # Open wrapper with base typography
    html_parts.append(
        '<section style="font-family: -apple-system, BlinkMacSystemFont, '
        '"PingFang SC", "Helvetica Neue", STHeiti, "Microsoft YaHei", '
        'sans-serif; font-size: 16px; line-height: 1.8; color: #333333; '
        'padding: 0 4px;">'
    )

    in_blockquote = False

    for line in lines:
        s = line.strip()
        if not s:
            if in_blockquote:
                html_parts.append('</section>')
                in_blockquote = False
            continue

        # Skip title comment
        if s.startswith('<!-- TITLE:'):
            continue

        # Image (already replaced from markdown by article_to_html,
        # so we handle inline ![]() for any missed ones only)
        if '![' in s:
            html_parts.append(s)
            continue

        # Thematic break
        if s == '---' or s == '___' or s == '***':
            if in_blockquote:
                html_parts.append('</section>')
                in_blockquote = False
            html_parts.append(
                '<hr style="border: none; border-top: 1px solid #e0e0e0; '
                'margin: 28px 0;">'
            )
            continue

        # Blockquote
        if s.startswith('> '):
            if not in_blockquote:
                if html_parts and html_parts[-1].startswith('<p>'):
                    pass  # close previous paragraph before blockquote
                html_parts.append(
                    '<section style="border-left: 4px solid #7c3aed; '
                    'background-color: #f5f0ff; padding: 12px 16px; '
                    'margin: 16px 0; font-size: 15px; color: #555; '
                    'border-radius: 0 6px 6px 0;">'
                )
                in_blockquote = True
            html_parts.append('<p style="margin: 4px 0;">' + s[2:] + '</p>')
            continue

        # Close blockquote if we were in one
        if in_blockquote:
            html_parts.append('</section>')
            in_blockquote = False

        # Heading 2
        if s.startswith('## '):
            html_parts.append(
                '<h2 style="font-size: 18px; font-weight: bold; '
                'color: #1a1a1a; margin: 28px 0 12px 0; line-height: 1.5;">'
                + s[3:] + '</h2>'
            )
            continue

        # Heading 3
        if s.startswith('### '):
            html_parts.append(
                '<h3 style="font-size: 17px; font-weight: bold; '
                'color: #2c2c2c; margin: 20px 0 8px 0; line-height: 1.5;">'
                + s[4:] + '</h3>'
            )
            continue

        # Unordered list item
        if s.startswith('- ') or s.startswith('* '):
            html_parts.append('<li>' + s[2:] + '</li>')
            continue

        # Regular paragraph: apply bold/code inline formatting
        text = s
        # Remove brackets from numbered section headers like 一、
        text = re.sub(
            r'^<strong>(一|二|三|四|五|六|七|八|九|十)[、.]',
            r'<strong>\1、', text
        )
        html_parts.append(
            '<p style="margin: 8px 0; letter-spacing: 0.5px; text-align: justify;">'
            + text + '</p>'
        )

    # Close blockquote if still open
    if in_blockquote:
        html_parts.append('</section>')

    # Wrap consecutive <li> into <ul>
    result = '\n'.join(html_parts)
    result = re.sub(
        r'(<li>.*?</li>(\s*<li>.*?</li>)*)',
        r'<ul style="padding-left: 1.5em; margin: 8px 0; list-style: disc;">\1</ul>',
        result, flags=re.DOTALL
    )
    # Convert bold markdown to <strong>
    result = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', result)
    # Close the wrapper section
    result += '\n</section>'
    return result

def article_to_html(md_path, images_dir, token):
    """Convert markdown to WeChat-compatible HTML with uploaded images."""
    raw = Path(md_path).read_text(encoding='utf-8')
    def replace_img(m):
        src = m.group(2)
        basename = os.path.basename(src)
        img_path = Path(images_dir) / basename
        if img_path.exists():
            try:
                url = upload_image_get_url(token, str(img_path))
                return (
                    '<p style="text-align: center; margin: 20px 0;">'
                    '<img src="' + url + '" alt="' + m.group(1) + '" '
                    'style="width: 100%; max-width: 100%; border-radius: 8px; '
                    'display: block; margin: 0 auto;" />'
                    '</p>'
                )
            except Exception as e:
                print("  warning: failed to upload " + basename + ": " + str(e))
        return m.group(0)
    raw = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_img, raw)
    return _md_to_html(raw)

def create_draft(token, title, html_content, digest, cover_media_id, author='AI'):
    """Create a WeChat draft. Send JSON as \\uXXXX-escaped ASCII to avoid encoding issues."""
    body = {
        'articles': [{
            'title': title,
            'author': author,
            'digest': digest,
            'content': html_content,
            'thumb_media_id': cover_media_id,
            'need_open_comment': 1,
            'only_fans_can_comment': 0,
        }]
    }
    # Send with ensure_ascii=False so Chinese characters stay as-is (not \uXXXX).
    payload = json.dumps(body, ensure_ascii=False)
    r = requests.post(
        'https://api.weixin.qq.com/cgi-bin/draft/add',
        params={'access_token': token},
        data=payload.encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'}
    )
    data = r.json()
    if 'media_id' in data:
        return data['media_id']
    raise Exception("create_draft failed: " + json.dumps(data, ensure_ascii=False))

def update_url_status_in_file(url: str, new_status: str = "used"):
    """Update a URL's status in all_urls.tsv from collected -> used.

    Preserves duplicates in the file but updates all matching lines.
    """
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
        print(f"  Marked {changed} entry/entries as '{new_status}' for: {url}")


def main():
    articles_dir = Path.home() / '.openclaw' / 'workspace' / 'wechat-articles'
    dirs = sorted([d for d in articles_dir.iterdir() if d.is_dir()])
    if not dirs:
        print("No article directories found!")
        sys.exit(1)
    
    target = os.environ.get('ARTICLE_DIR')
    if target:
        latest = articles_dir / target
    else:
        latest = dirs[-1]
    md_path = latest / 'article.md'
    img_dir = latest / 'images'
    meta_path = latest / 'meta.json'
    
    print("Publishing: " + latest.name)
    
    # Read title/digest from meta.json
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    title = meta.get('title', '')
    digest = meta.get('digest', '')
    if not title:
        md_text = md_path.read_text(encoding='utf-8')
        m = re.search(r'^# (.+)', md_text, re.MULTILINE)
        title = m.group(1).strip() if m else 'AI'
    
    # WeChat API: title max 64 bytes (~21 Chinese chars in UTF-8).
    # Soft target: keep under 25 chars for list display; truncate gracefully.
    MAX_TITLE_CHARS = 25
    MAX_DIGEST_CHARS = 20  # WeChat digest limit
    title = title.replace('：', '-').replace(':', '-')
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1] + '…'
    if len(digest) > MAX_DIGEST_CHARS:
        digest = digest[:MAX_DIGEST_CHARS - 1] + '…'
    
    print("  Title: " + title + " (" + str(len(title)) + "c)")
    print("  Digest: " + digest + " (" + str(len(digest)) + "c)")
    
    # Check limits before calling API
    title_bytes = len(title.encode('utf-8'))
    if title_bytes > 64:
        # Truncate by bytes, careful with multi-byte chars
        while len(title.encode('utf-8')) > 64:
            title = title[:-1]
        print("  Title re-truncated to " + str(len(title)) + " chars (" + str(len(title.encode('utf-8'))) + " bytes)")
    if len(digest) > MAX_DIGEST_CHARS:
        print("ERROR: digest too long: " + str(len(digest)) + " chars")
        sys.exit(1)
    
    env = load_env()
    app_id = env.get('WECHAT_APP_ID')
    app_secret = env.get('WECHAT_APP_SECRET')
    if not app_id or not app_secret:
        print("ERROR: credentials not found in .env")
        sys.exit(1)
    
    print("Getting access token...")
    token = get_token(app_id, app_secret)
    
    print("Uploading cover image...")
    cover_path = (img_dir / 'cover-1x1.jpg') if (img_dir / 'cover-1x1.jpg').exists() else (img_dir / 'cover-wide.jpg')
    cover_media_id = upload_image(token, str(cover_path))
    print("  Cover uploaded: " + cover_media_id[:20] + "...")
    
    # Prepend brand template header
    tmp_md_text = md_path.read_text(encoding='utf-8')
    tmp_md_text = _add_header_template(tmp_md_text, title)
    tmp_path = md_path.with_suffix('.md.tmp')
    tmp_path.write_text(tmp_md_text, encoding='utf-8')
    
    print("Converting article to HTML...")
    html_content = article_to_html(tmp_path, img_dir, token)
    # Clean up temp file
    if tmp_path.exists():
        tmp_path.unlink()
    print("  HTML: " + str(len(html_content)) + " chars")
    
    print("Creating WeChat draft...")
    media_id = create_draft(token, title, html_content, digest, cover_media_id)
    print("  Draft created: " + media_id)
    
    # Save result
    (latest / 'wechat_result.json').write_text(json.dumps({
        'title': title, 'digest': digest, 'media_id': media_id,
        'published_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False, indent=2))
    
    # Mark URLs used in this article as 'used' in the registry
    meta_urls = meta.get('sources', []) or []
    md_text = md_path.read_text(encoding='utf-8')
    # Also scan article text for URLs
    found_urls = re.findall(r'https?://[^\s<\)\]]+', md_text)
    all_urls = list(set(meta_urls + found_urls))
    for u in all_urls:
        u = u.rstrip(')').rstrip('.')  # clean trailing punctuation
        if u.startswith('http'):
            update_url_status_in_file(u, 'used')
    if all_urls:
        print(f"  Marked {len(all_urls)} source URL(s) as used in registry")
    
    print("\nDone! Title: " + title)

if __name__ == '__main__':
    main()
