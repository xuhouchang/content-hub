"""Clustering module for curating materials into topic clusters.

Tokenizes title + summary using simple word matching against priority tokens,
with HTML stripping to avoid content pollution.
"""

import os
import re

# Minimum content chars before falling back to Jina.ai fetch
_JINA_CONTENT_MIN = 2000

STOPWORDS = {
    "a",
    "about",
    "and",
    "are",
    "but",
    "for",
    "from",
    "how",
    "in",
    "inside",
    "into",
    "is",
    "it",
    "more",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "them",
    "these",
    "they",
    "this",
    "to",
    "was",
    "what",
    "why",
    "will",
    "with",
    "you",
}

# Markers too broad to form useful cluster seeds.
GENERIC_TOKENS = {
    "ai",
    "new",
    "agent",
    "agents",
    "model",
    "models",
    "build",
    "builds",
    "building",
    "data",
    "make",
    "making",
    "work",
    "using",
    "use",
    "used",
    "based",
    "big",
    "need",
    "needs",
    "time",
    "way",
    "ways",
    "know",
    "now",
    "get",
    "set",
    "first",
    "one",
    "see",
    "like",
    "also",
    "just",
    "even",
    "much",
    "still",
    "well",
    "help",
    "could",
    "can",
}

CANONICAL_TOKEN_MAP = {
    "adoption": "adoption",
    "copilot": "copilot",
    "design": "workflow",
    "enterprises": "enterprise",
    "enterprise": "enterprise",
    "governance": "governance",
    "openai": "openai",
    "ops": "operations",
    "operations": "operations",
    "procurement": "procurement",
    "resistance": "resistance",
    "scaling": "scaling",
    "search": "search",
    "teams": "team",
    "tools": "tool",
    "training": "training",
    "workflow": "workflow",
}

PRIORITY_CLUSTER_TOKENS = [
    "adoption",
    "workflow",
    "enterprise",
    "resistance",
    "governance",
    "procurement",
    "permission",
    "budget",
    "manager",
    "team",
    "search",
    "copilot",
    "operations",
    "scaling",
    "training",
    "openai",
    "tool",
]

# HTML entity pattern: &...;
_HTML_ENTITY_RE = re.compile(r"&[a-zA-Z#]+;")
# HTML tag pattern: <...>
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML entities and tags from text."""
    text = _HTML_ENTITY_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    return text


def _normalize_token(token: str) -> str:
    value = token.strip(" ,.!?:;()[]{}\"'`$#%&*-_=+/\\|@~").lower()
    value = CANONICAL_TOKEN_MAP.get(value, value)
    if value.endswith("s") and len(value) > 4 and value not in {"analysis", "business", "process"}:
        value = value[:-1]
    return value


def _tokenize(text: str) -> list[str]:
    """Tokenize cleaned text, returning meaningful non-stopword tokens."""
    cleaned = _strip_html(text)
    tokens = []
    for raw_token in cleaned.lower().split():
        token = _normalize_token(raw_token)
        if token and token not in STOPWORDS and token not in GENERIC_TOKENS:
            tokens.append(token)
    return tokens


def _load_jina_key() -> str:
    """Load JINA_API_KEY from env, .env file, or sources.yaml."""
    key = os.environ.get("JINA_API_KEY", "")
    if key:
        return key
    # Try loading from .env file (workspace root)
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("JINA_API_KEY="):
                    key = line.split("=", 1)[1].strip('"\'')
                    if key:
                        return key
    # Try sources.yaml fallback
    try:
        import yaml
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sources.yaml")
        if os.path.isfile(src_path):
            with open(src_path) as f:
                cfg = yaml.safe_load(f)
            jina_cfg = cfg.get("reader", {}).get("jina", {})
            if jina_cfg.get("enabled", False):
                k = jina_cfg.get("api_key", "")
                if k and k != "your_jina_api_key_here":
                    return k
    except Exception:
        pass
    return ""


def _fetch_jina(url: str) -> str | None:
    """Fetch a URL via Jina.ai Reader API, returning markdown text."""
    api_key = _load_jina_key()
    if not api_key:
        return None
    try:
        import requests
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [cluster] Jina fetch failed for {url[:60]}: {e}")
        return None


def _cluster_seed(material: dict) -> str:
    tags = material.get("tags", {})
    topic_focus = tags.get("topic_focus", [])

    # Check if we have enough content for meaningful clustering
    content_text = material.get("content_text", "")
    text_source = "default"

    if len(content_text) < _JINA_CONTENT_MIN:
        url = material.get("canonical_url", material.get("url", ""))
        if url:
            jina_body = _fetch_jina(url)
            if jina_body and len(jina_body) > _JINA_CONTENT_MIN:
                content_text = _strip_html(jina_body)
                text_source = "jina"

    # Build text from title + content_text
    text = " ".join(
        [
            material.get("title", ""),
            content_text,
            " ".join(topic_focus),
        ]
    )
    tokens = _tokenize(text)

    # 1) Priority token match — these are the most meaningful cluster signals
    priority_hits = []
    seen = set()
    for tok in tokens:
        if tok in PRIORITY_CLUSTER_TOKENS and tok not in seen:
            priority_hits.append(tok)
            seen.add(tok)

    if priority_hits:
        # Sort so materials sharing the same priority-token set always map to the
        # same cluster id regardless of token order in the source text.
        seed = sorted(priority_hits)[:2]
        return "cluster_" + "_".join(seed)

    # 2) Include title + summary to find shared thematic words
    title_tokens = _tokenize(material.get("title", ""))
    summary_tokens = _tokenize(material.get("summary", ""))

    # Use tokens that appear in BOTH title and summary (shared emphasis)
    shared = set(title_tokens) & set(summary_tokens)
    meaningful = [t for t in shared if len(t) > 3] if shared else []

    if meaningful:
        seed = sorted(meaningful)[:2]
        return "cluster_" + "_".join(seed)

    # 3) Fallback: most distinctive tokens from title
    all_meaningful = [t for t in title_tokens if len(t) > 3]
    if all_meaningful:
        seed = sorted(set(all_meaningful))[:2]
        return "cluster_" + "_".join(seed)

    # 4) Last resort: any distinct tokens
    if tokens:
        seed = sorted(set(tokens))[:2]
        return "cluster_" + "_".join(seed)

    return "cluster_misc"


def assign_clusters(materials: list[dict]) -> list[dict]:
    """Assign cluster_id to each material's dedup dict."""
    clustered = []
    for material in materials:
        updated = dict(material)
        dedup = dict(updated.get("dedup", {}))
        dedup["cluster_id"] = _cluster_seed(updated)
        dedup["is_primary"] = dedup.get("is_primary", True)
        updated["dedup"] = dedup
        clustered.append(updated)
    return clustered
