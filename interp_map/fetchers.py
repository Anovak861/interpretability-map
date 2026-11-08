"""Pull raw documents from each source into a common `Fetched` record.

Fetchers only gather metadata, text and explicit references. Summaries,
concepts and claims come later from `extract.py` (or a hand-written
extraction), so a fetch is cheap and can be re-run.
"""
from __future__ import annotations

import io
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import DocumentMeta, ReferenceRef

USER_AGENT = "interp-map/0.1 (personal research index)"
_client = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT})

# arXiv asks for ~3 s between API calls; bulk runs that ignore this get 429s and 503s
_MIN_INTERVAL = {"export.arxiv.org": 3.1, "arxiv.org": 1.0}
_last_call: dict[str, float] = {}

# Citation enrichment is optional. A shared cooldown avoids repeatedly retrying
# a depleted Semantic Scholar pool for every arXiv document in the same process.
_S2_COOLDOWN_SECONDS = 15 * 60
_s2_retry_at = 0.0
_s2_lock = threading.Lock()


def polite_get(url: str, *, params=None, retries: int = 4) -> httpx.Response:
    """GET with per-host spacing and backoff on 429/503 (honouring Retry-After)."""
    host = urlparse(url).netloc.replace("www.", "")
    for attempt in range(retries + 1):
        gap = _MIN_INTERVAL.get(host, 0) - (time.time() - _last_call.get(host, 0))
        if gap > 0:
            time.sleep(gap)
        _last_call[host] = time.time()
        r = _client.get(url, params=params)
        if r.status_code not in (429, 503) or attempt == retries:
            return r
        retry_after = r.headers.get("retry-after", "")
        time.sleep(float(retry_after) if retry_after.isdigit() else 5 * 2 ** attempt)
    return r


def arxiv_metadata(ids: list[str]) -> dict[str, dict]:
    """Title, authors and date for many arXiv ids at once (100 per request)."""
    out = {}
    for i in range(0, len(ids), 100):
        r = polite_get("https://export.arxiv.org/api/query",
                       params={"id_list": ",".join(ids[i:i + 100]), "max_results": 100})
        r.raise_for_status()
        for e in ET.fromstring(r.text).findall("a:entry", ATOM):
            m = re.search(r"(\d{4}\.\d{4,5})", e.findtext("a:id", "", ATOM))
            title = " ".join(e.findtext("a:title", "", ATOM).split())
            if m and title and title.lower() != "error":
                out[m.group(1)] = {"title": title, "published_date": e.findtext("a:published", "", ATOM)[:10] or None,
                                   "authors": [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)]}
    return out


@dataclass
class Fetched:
    meta: DocumentMeta
    text: str
    references: list[ReferenceRef] = field(default_factory=list)
    external_ids: dict = field(default_factory=dict)


def fetch(url: str) -> Fetched:
    host = urlparse(url).netloc.replace("www.", "")
    if host == "arxiv.org":
        return fetch_arxiv(url)
    if host in ("lesswrong.com", "alignmentforum.org"):
        return fetch_lesswrong(url)
    if host in ("x.com", "twitter.com"):
        raise ValueError("Twitter/X can't be fetched automatically; save the thread text to a file "
                         "and use `add-manual` (see README).")
    return fetch_web(url)


# ---------------------------------------------------------------- arXiv

ATOM = {"a": "http://www.w3.org/2005/Atom"}


def arxiv_id_from(url_or_id: str) -> str:
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", url_or_id)
    if not m:
        raise ValueError(f"not an arXiv id: {url_or_id}")
    return m.group(1)


def arxiv_id_or_none(url: Optional[str]) -> Optional[str]:
    if not url or "arxiv.org" not in url:
        return None
    m = re.search(r"(\d{4}\.\d{4,5})", url)
    return m.group(1) if m else None


def fetch_arxiv(url_or_id: str) -> Fetched:
    aid = arxiv_id_from(url_or_id)
    r = polite_get("https://export.arxiv.org/api/query", params={"id_list": aid}, retries=1)
    entry = ET.fromstring(r.text).find("a:entry", ATOM) if r.status_code == 200 else None
    if entry is not None:
        meta = DocumentMeta(
            title=" ".join(entry.findtext("a:title", "", ATOM).split()),
            doc_type="paper",
            source="arxiv",
            url=f"https://arxiv.org/abs/{aid}",
            arxiv_id=aid,
            published_date=entry.findtext("a:published", "", ATOM)[:10] or None,
            authors=[a.findtext("a:name", "", ATOM) for a in entry.findall("a:author", ATOM)],
            abstract=" ".join(entry.findtext("a:summary", "", ATOM).split()),
        )
    else:
        # the export API throttles bulk use hard; the abstract page carries the same metadata
        meta = _arxiv_meta_from_abs_page(aid)
    text = _arxiv_full_text(aid) or meta.abstract or ""
    refs, s2 = semantic_scholar_references(aid)
    return Fetched(meta, text, refs, {"s2": s2} if s2 else {})


def _arxiv_meta_from_abs_page(aid: str) -> DocumentMeta:
    r = polite_get(f"https://arxiv.org/abs/{aid}")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title = _meta(soup, "citation_title")
    if not title:
        raise ValueError(f"no arXiv metadata found for {aid}")
    abstract = soup.select_one("blockquote.abstract")
    return DocumentMeta(
        title=" ".join(title.split()),
        doc_type="paper",
        source="arxiv",
        url=f"https://arxiv.org/abs/{aid}",
        arxiv_id=aid,
        published_date=_iso_date(_meta(soup, "citation_date") or _meta(soup, "citation_online_date")),
        authors=[_first_last(t["content"]) for t in soup.find_all("meta", attrs={"name": "citation_author"})],
        abstract=" ".join(abstract.get_text(" ").replace("Abstract:", "", 1).split()) if abstract else None,
    )


def _complete_arxiv_html(html: str) -> bool:
    """Reject visibly partial conversions before treating HTML as a full paper.

    BeautifulSoup repairs unclosed markup, so check raw termination first. A
    bibliography is a conservative completeness signal; papers lacking one use
    the authoritative PDF instead. This does not prove every equation rendered.
    """
    if not re.search(r"</body\s*>\s*</html\s*>\s*$", html, re.I):
        return False
    soup = BeautifulSoup(html, "html.parser")
    document = soup.select_one(".ltx_document")
    if document is None or not document.select_one(".ltx_bibliography .ltx_bibitem"):
        return False
    if soup.select_one(".ltx_ERROR, .ltx_error, .ltx_conversion_error"):
        return False
    if re.search(r"(?:latexml conversion failed|conversion timed out|incomplete conversion)",
                 soup.get_text(" "), re.I):
        return False
    return True


def _arxiv_full_text(aid: str) -> Optional[str]:
    # arXiv's HTML rendering is cleaner than PDF extraction when it exists
    r = polite_get(f"https://arxiv.org/html/{aid}")
    if r.status_code == 200 and _complete_arxiv_html(r.text):
        return html_to_text(r.text)
    r = polite_get(f"https://arxiv.org/pdf/{aid}")
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return None


def semantic_scholar_references(aid: str) -> tuple[list[ReferenceRef], Optional[str]]:
    """Explicit citations from Semantic Scholar. The unauthenticated pool is shared and
    often rate-limited; set S2_API_KEY (free on request) for reliable access."""
    import os
    import sys

    global _s2_retry_at
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{aid}"
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.environ.get("S2_API_KEY") else {}
    # Serialize only this optional service so simultaneous callers cannot race
    # past the first 429 and keep sending requests during its cooldown.
    with _s2_lock:
        if time.monotonic() < _s2_retry_at:
            print(f"  warning: Semantic Scholar references unavailable for {aid} "
                  "(rate-limit cooldown); run `cli.py backfill-refs` later", file=sys.stderr)
            return [], None
        r = _client.get(url, headers=headers,
                        params={"fields": "paperId,references.title,references.externalIds,references.url"})
        if r.status_code == 429:
            _s2_retry_at = time.monotonic() + _S2_COOLDOWN_SECONDS
        if r.status_code != 200:
            print(f"  warning: Semantic Scholar references unavailable for {aid} (HTTP {r.status_code}); "
                  "run `cli.py backfill-refs` later", file=sys.stderr)
            return [], None
        body = r.json()
        refs = []
        for p in body.get("references") or []:
            if not p.get("title"):
                continue
            ext = p.get("externalIds") or {}
            refs.append(ReferenceRef(title=p["title"], arxiv_id=ext.get("ArXiv"),
                                     url=f"https://arxiv.org/abs/{ext['ArXiv']}" if ext.get("ArXiv") else p.get("url")))
        return refs, body.get("paperId")


# ---------------------------------------------------------------- LessWrong / Alignment Forum

_LW_QUERY = """query ($id: String) { post(input: {selector: {_id: $id}}) { result {
  _id title postedAt pageUrl af htmlBody user { displayName } coauthors { displayName } } } }"""


def fetch_lesswrong(url: str) -> Fetched:
    m = re.search(r"/posts/([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError(f"not a LessWrong/AF post URL: {url}")
    host = "alignmentforum.org" if "alignmentforum" in url else "lesswrong.com"
    r = _client.post(f"https://www.{host}/graphql", json={"query": _LW_QUERY, "variables": {"id": m.group(1)}})
    r.raise_for_status()
    post = r.json()["data"]["post"]["result"]
    html = post.get("htmlBody") or ""
    meta = DocumentMeta(
        title=post["title"],
        doc_type="blog_post",
        source="alignmentforum" if host == "alignmentforum.org" or post.get("af") else "lesswrong",
        url=f"https://{host}/posts/{post['_id']}",
        published_date=(post.get("postedAt") or "")[:10] or None,
        authors=[post["user"]["displayName"]] + [c["displayName"] for c in post.get("coauthors") or []],
    )
    return Fetched(meta, html_to_text(html), links_as_references(html, meta.url), {"lw_post_id": post["_id"]})


# ---------------------------------------------------------------- generic web (incl. transformer-circuits.pub)

def fetch_web(url: str) -> Fetched:
    r = _client.get(url)
    r.raise_for_status()
    # Redirect destinations determine relative links and canonical identity.
    url = str(r.url)
    if "application/pdf" in r.headers.get("content-type", "").lower() or r.content.lstrip().startswith(b"%PDF-"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        title = str((reader.metadata or {}).get("/Title") or url)
        return Fetched(DocumentMeta(title=title, doc_type="paper", source="web", url=url),
                       "\n".join(page.extract_text() or "" for page in reader.pages), [])
    soup = BeautifulSoup(r.text, "html.parser")
    title = (_meta(soup, "citation_title") or _meta(soup, "og:title")
             or (soup.title.string.strip() if soup.title and soup.title.string else url))
    authors = [_first_last(t["content"]) for t in soup.find_all("meta", attrs={"name": "citation_author"})]
    date = _meta(soup, "citation_publication_date") or _meta(soup, "article:published_time")
    if "transformer-circuits.pub" in url:
        authors = authors or _distill_authors(soup)
        date = date or _distill_date(soup)
    meta = DocumentMeta(
        title=title,
        doc_type="paper" if "transformer-circuits.pub" in url else "blog_post",
        source="transformer_circuits" if "transformer-circuits.pub" in url else "web",
        url=url,
        published_date=_iso_date(date),
        authors=authors,
    )
    text = html_to_text(r.text)
    # Conference landing pages often contain only an abstract; follow their official PDF.
    pdf_url = _meta(soup, "citation_pdf_url")
    if not pdf_url and "openreview.net/forum" in url:
        pdf_url = url.replace("/forum", "/pdf", 1)
    if pdf_url:
        pdf = _client.get(urljoin(url, pdf_url))
        pdf.raise_for_status()
        if "application/pdf" in pdf.headers.get("content-type", "").lower() or pdf.content.lstrip().startswith(b"%PDF-"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf.content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if not text.strip():
                raise ValueError("advertised PDF has no extractable text")
            citation_title = _meta(soup, "citation_title")
            if citation_title:
                normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
                if normalize(citation_title) not in normalize(text[:2000]):
                    raise ValueError("advertised PDF opening does not match the landing page citation title")
            meta.doc_type = "paper"
        else:
            raise ValueError("advertised PDF returned non-PDF content; full text unavailable")
    return Fetched(meta, text, links_as_references(r.text, url))


def _first_last(name: str) -> str:
    """citation_author tags are often "Olah, Chris"; store "Chris Olah" so names match other sources."""
    if name.count(",") == 1:
        last, first = (x.strip() for x in name.split(","))
        if first and last:
            return f"{first} {last}"
    return name.strip()


def _iso_date(value: Optional[str]) -> Optional[str]:
    """Citation meta tags use '2020/03/10' or full timestamps; store plain ISO dates."""
    if not value:
        return None
    m = re.match(r"(\d{4})[/-](\d{1,2})(?:[/-](\d{1,2}))?", value.strip())
    if not m:
        return value
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3) or 1)
    return f"{y}-{mo:02d}-{d:02d}"


def _meta(soup, name) -> Optional[str]:
    tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return tag["content"].strip() if tag and tag.get("content") else None


def _distill_authors(soup) -> list[str]:
    # transformer-circuits.pub uses a Distill-style byline with footnote marks in <sup>
    names = []
    for a in soup.select(".byline .author, d-byline .author, .authors .author"):
        for sup in a.find_all("sup"):
            sup.decompose()
        name = " ".join(a.get_text(" ").split()).strip(" ,*")
        if name:
            names.append(name)
    return names


def _distill_date(soup) -> Optional[str]:
    from datetime import datetime
    el = soup.select_one(".published div, d-byline .date")
    if not el:
        return None
    text = el.get_text(" ", strip=True)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- manual (tweets, talks, anything else)

def fetch_manual(path: Path, *, title: str, url: Optional[str], authors: list[str],
                 doc_type: str = "tweet_thread", date: Optional[str] = None) -> Fetched:
    source = "twitter" if url and re.search(r"(x|twitter)\.com", url) else "manual"
    meta = DocumentMeta(title=title, doc_type=doc_type, source=source, url=url,
                        published_date=date, authors=authors)
    text = path.read_text(encoding="utf-8")
    refs = [ReferenceRef(title=u, url=u) for u in re.findall(r"https?://\S+", text)
            if "arxiv.org" in u or "lesswrong.com" in u or "alignmentforum.org" in u]
    return Fetched(meta, text, refs)


# ---------------------------------------------------------------- helpers

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    # LaTeXML can render entire prose/prompt boxes as SVG foreignObject HTML.
    # Removing SVG wholesale silently deletes their content (e.g. arXiv
    # 2610.29129, appendices J.1/J.8). Preserve text, discard drawing markup.
    for svg in soup.find_all("svg"):
        if svg.parent is not None:
            svg.replace_with(svg.get_text("\n"))
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_RESEARCH_HOSTS = ("arxiv.org", "lesswrong.com", "alignmentforum.org", "transformer-circuits.pub", "distill.pub")
# paths that are actual papers/posts rather than site chrome or supplementary pages
_PAPER_PATH = {
    "arxiv.org": re.compile(r"/(abs|pdf|html)/\d{4}\.\d{4,5}"),
    "lesswrong.com": re.compile(r"/posts/[A-Za-z0-9]+"),
    "alignmentforum.org": re.compile(r"/posts/[A-Za-z0-9]+"),
    "transformer-circuits.pub": re.compile(r"^/20\d\d/[^/]+/(index\.html)?$"),
    "distill.pub": re.compile(r"^/20\d\d/[^/]+(/[^/]+)?/?$"),
}


def links_as_references(html: str, base_url: str) -> list[ReferenceRef]:
    """Outbound links to research papers/posts count as explicit citations."""
    from .db import normalize_url

    base = normalize_url(base_url)
    soup = BeautifulSoup(html, "html.parser")
    seen, refs = {base}, []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        parsed = urlparse(href)
        host = parsed.netloc.replace("www.", "")
        pattern = _PAPER_PATH.get(host)
        if not pattern or not pattern.search(parsed.path):
            continue
        url = normalize_url(href)
        if url in seen:
            continue
        seen.add(url)
        label = " ".join(a.get_text(" ").split())
        aid = re.search(r"(\d{4}\.\d{4,5})", url).group(1) if host == "arxiv.org" else None
        # link text is often "[12]" or "this post"; fall back to the URL as a stub title
        refs.append(ReferenceRef(title=label if len(label) > 20 else url, url=url, arxiv_id=aid))
    return refs
