#!/usr/bin/env python3
"""
gen-books.py — scan an explicit allowlist of project repos for book
citations (ISBN lines, and italic-title + known-publisher citations) and
emit _data/books.yml for the Jekyll site's Books page.

Standalone, stdlib-only. Designed to run clean under `python3 -I`
(isolated mode: ignores PYTHONPATH / user site-packages / the script's own
directory being added ahead of stdlib). Never imports anything from the
scanned repo directories.

Usage:
    python3 -I scripts/gen-books.py --repos-root /home/rsedor/Dev --out _data/books.yml

The output is machine-generated and flagged for human review: the
extraction is heuristic text-mining of markdown/asciidoc prose, not a
real bibliographic parser.
"""

import argparse
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ----------------------------------------------------------------------
# Explicit allowlist of repo slugs under --repos-root. We never glob the
# whole root directory — only these exact names are scanned.
# ----------------------------------------------------------------------
ALLOWED_REPOS = [
    "datamesh-reference-arch-quarkus",
    "datamesh-reference-arch-python",
    "cloud-native-design-patterns",
    "minikube-on-fedora",
    "domain-driven-design-observability-workshop",
    "modernizing-enterprise-applications",
    "enterprise-integration-patterns-with-camel",
    "quarkus-optimization",
    "spring-boot-optimization",
    "cpp-container-optimization-tutorial",
    "ebpf-with-aya",
    "linux-systems-programming",
    "lgtm-skills",
    "hummingbird-tutorial",
    "coloringbooks",
]

# Directory fragments that disqualify a path from being scanned, even
# inside an allowlisted repo.
EXCLUDE_FRAGMENTS = (
    "/_site/",
    "/node_modules/",
    "/vendor/",
    "/.git/",
    "/optimizing-java/",
)

SCAN_SUFFIXES = (".md", ".adoc")

# ----------------------------------------------------------------------
# Regexes
# ----------------------------------------------------------------------

# Tier 1: an explicit ISBN mention.
ISBN_RE = re.compile(r"ISBN(?:-1[03])?\s*:?\s*([0-9][0-9\-\s]{8,}[0-9xX])", re.IGNORECASE)

# Single-star italic *Title*, but not part of a **bold** run.
TITLE_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)")

# Underscore italic _Title_. Require an internal space so we don't match
# snake_case identifiers like `file_name.py`.
TITLE_UNDERSCORE_RE = re.compile(r"(?<!_)_([A-Z][^_]*\s[^_]*?)_(?!_)")

# Known publisher tokens -> canonical display name.
PUBLISHER_TOKENS = [
    (r"O['’]Reilly", "O'Reilly"),
    (r"Addison-Wesley", "Addison-Wesley"),
    (r"Addison Wesley", "Addison-Wesley"),
    (r"Pragmatic Bookshelf", "Pragmatic Bookshelf"),
    (r"Pragmatic Programmer", "Pragmatic Bookshelf"),
    (r"Pragmatic", "Pragmatic Bookshelf"),
    (r"No Starch", "No Starch Press"),
    (r"Manning", "Manning"),
    (r"Packt", "Packt"),
    (r"Wiley", "Wiley"),
    (r"Apress", "Apress"),
]
PUBLISHER_RE = re.compile("|".join(f"({p})" for p, _ in PUBLISHER_TOKENS))

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

FURTHER_READING_HEADING_RE = re.compile(
    r"further reading|bibliograph|references|recommended reading", re.IGNORECASE
)

BULLET_PREFIX_RE = re.compile(r"^[\s\-\*•\d\.\)]+")

AUTHOR_DASH_RE = re.compile(
    r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,4})\s*(?:--|—|–)\s*$"
)
AUTHOR_POSSESSIVE_RE = re.compile(
    r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,4})'s\s*$"
)


def normalize_key(s):
    """Lowercase, strip punctuation/whitespace, for dedup keys."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def normalize_isbn(raw):
    digits = re.sub(r"[\-\s]", "", raw).upper()
    return digits


def canonical_publisher(line):
    m = PUBLISHER_RE.search(line)
    if not m:
        return None
    text = m.group(0)
    for pattern, canonical in PUBLISHER_TOKENS:
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return canonical
    return text


def is_plausible_title(candidate):
    """Reject obvious mis-parses.

    Markdown emphasis can span a hard-wrapped line boundary (an opening
    `*` on one physical line, closing `*` several words into the next),
    which otherwise produces nonsense "titles" like
    "(O'Reilly) for the boundary tools and". A real book title doesn't
    contain a parenthesized publisher clause, doesn't start lowercase,
    and isn't absurdly long.
    """
    candidate = candidate.strip()
    if not candidate or len(candidate) > 100:
        return False
    if not re.match(r"^[A-Z0-9\"'‘’]", candidate):
        return False
    if "(" in candidate or ")" in candidate:
        return False
    if PUBLISHER_RE.search(candidate):
        return False
    return True


def extract_title(segment):
    for m in TITLE_STAR_RE.finditer(segment):
        candidate = m.group(1).strip()
        if is_plausible_title(candidate):
            return candidate, m.start()
    for m in TITLE_UNDERSCORE_RE.finditer(segment):
        candidate = m.group(1).strip()
        if is_plausible_title(candidate):
            return candidate, m.start()
    return None, None


def clean_author_prefix(prefix):
    prefix = BULLET_PREFIX_RE.sub("", prefix)
    prefix = prefix.replace("**", "")
    return prefix.strip()


def extract_author(prefix):
    prefix = clean_author_prefix(prefix)
    if not prefix:
        return None

    m = AUTHOR_DASH_RE.search(prefix)
    if m:
        return m.group(1).strip()

    m = AUTHOR_POSSESSIVE_RE.search(prefix)
    if m:
        return m.group(1).strip()

    if len(prefix) <= 60 and re.match(r"^[A-Z]", prefix):
        return prefix.rstrip(",").strip()

    words = prefix.split()
    if words:
        candidate = " ".join(words[-5:])
        if re.match(r"^[A-Z]", candidate):
            return candidate.rstrip(",").strip()

    return None


def extract_year(segment, exclude_span=None):
    for m in YEAR_RE.finditer(segment):
        if exclude_span and exclude_span[0] <= m.start() < exclude_span[1]:
            continue
        return int(m.group(0))
    return None


def extract_url(line):
    m = MD_LINK_RE.search(line)
    if m:
        return m.group(2)
    return None


def first_author_key(authors):
    """Normalized surname of the first author, used as part of the dedup
    key when no ISBN is available. Using just the surname (rather than
    the whole first-author chunk) lets "Enberg" and "Pekka Enberg" (or
    "Evans, B. J." and "Evans") resolve to the same person."""
    if not authors:
        return ""
    chunk = authors.split("&")[0].split(" and ")[0].strip().rstrip(",")
    if "," in chunk:
        surname = chunk.split(",", 1)[0].strip()
    else:
        words = chunk.split()
        surname = words[-1] if words else chunk
    return normalize_key(surname)


# ----------------------------------------------------------------------
# --enrich: best-effort metadata enrichment against public, no-auth book
# APIs (Google Books, OpenLibrary). Stdlib-only (urllib/json), safe under
# `python3 -I`. Every network call is wrapped so that *any* failure
# (timeout, DNS, HTTP error, bad JSON, rate limiting, etc.) just skips
# that lookup — enrichment must never crash the generator.
# ----------------------------------------------------------------------

ENRICH_USER_AGENT = (
    "patterncatalyst-gen-books/1.0 "
    "(+https://github.com/patterncatalyst/patterncatalyst-workshops-tutorials-list; "
    "contact: repo maintainer)"
)
ENRICH_TIMEOUT = 8
ENRICH_REQUEST_DELAY = 0.15  # be polite to free, unauthenticated public APIs

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_ISBN_API = "https://openlibrary.org/isbn/{isbn}.json"
OPENLIBRARY_SEARCH_API = "https://openlibrary.org/search.json"
OPENLIBRARY_AUTHOR_API = "https://openlibrary.org{key}.json"

BARE_SURNAME_RE = re.compile(r"^[A-Z][A-Za-z'\-]+$")
SURNAME_INITIALS_RE = re.compile(r"^[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.\s*)+$")


def http_get_json(url):
    """GET a URL and parse it as JSON, or return None on ANY failure.

    Deliberately broad exception handling: this is a best-effort lookup
    against a third-party public API, and the surrounding enrichment pass
    must never crash the generator over a timeout, a DNS hiccup, a 429/5xx,
    or a malformed response body.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ENRICH_USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=ENRICH_TIMEOUT) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - intentional: network calls must not crash
        print(f"enrich: lookup failed, skipping ({url}): {exc}", file=sys.stderr)
        return None
    finally:
        time.sleep(ENRICH_REQUEST_DELAY)


def resolve_openlibrary_author(key):
    data = http_get_json(OPENLIBRARY_AUTHOR_API.format(key=key))
    if not data:
        return None
    return data.get("name")


def parse_google_books_item(data):
    if not isinstance(data, dict):
        return None
    items = data.get("items") or []
    if not items:
        return None
    vi = items[0].get("volumeInfo", {}) or {}
    isbn13 = isbn10 = None
    for ident in vi.get("industryIdentifiers", []) or []:
        if ident.get("type") == "ISBN_13":
            isbn13 = ident.get("identifier")
        elif ident.get("type") == "ISBN_10":
            isbn10 = ident.get("identifier")
    year = None
    ym = re.match(r"(\d{4})", vi.get("publishedDate") or "")
    if ym:
        year = int(ym.group(1))
    title = vi.get("title")
    if vi.get("subtitle"):
        title = f"{title}: {vi['subtitle']}" if title else vi["subtitle"]
    return {
        "title": title,
        "authors": vi.get("authors") or [],
        "publisher": vi.get("publisher"),
        "year": year,
        "isbn": isbn13 or isbn10,
        "source": "Google Books",
    }


def google_books_by_isbn(isbn):
    url = GOOGLE_BOOKS_API + "?" + urllib.parse.urlencode({"q": f"isbn:{isbn}"})
    return parse_google_books_item(http_get_json(url))


def google_books_by_title_author(title, author_surname):
    q = f"intitle:{title}"
    if author_surname:
        q += f" inauthor:{author_surname}"
    url = GOOGLE_BOOKS_API + "?" + urllib.parse.urlencode({"q": q})
    return parse_google_books_item(http_get_json(url))


def openlibrary_by_isbn(isbn):
    data = http_get_json(OPENLIBRARY_ISBN_API.format(isbn=urllib.parse.quote(isbn, safe="")))
    if not isinstance(data, dict):
        return None
    publishers = data.get("publishers") or []
    ym = re.search(r"(\d{4})", data.get("publish_date") or "")
    year = int(ym.group(1)) if ym else None
    authors = []
    for a in (data.get("authors") or [])[:4]:
        key = a.get("key")
        if key:
            name = resolve_openlibrary_author(key)
            if name:
                authors.append(name)
    isbn13_list = data.get("isbn_13") or []
    title = data.get("title")
    if data.get("subtitle"):
        title = f"{title}: {data['subtitle']}" if title else data["subtitle"]
    return {
        "title": title,
        "authors": authors,
        "publisher": publishers[0] if publishers else None,
        "year": year,
        "isbn": isbn13_list[0] if isbn13_list else isbn,
        "source": "OpenLibrary",
    }


def openlibrary_by_title_author(title, author_surname):
    params = {"title": title, "limit": "1"}
    if author_surname:
        params["author"] = author_surname
    url = OPENLIBRARY_SEARCH_API + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    if not isinstance(data, dict):
        return None
    docs = data.get("docs") or []
    if not docs:
        return None
    d = docs[0]
    isbns = d.get("isbn") or []
    return {
        "title": d.get("title"),
        "authors": d.get("author_name") or [],
        "publisher": (d.get("publisher") or [None])[0],
        "year": d.get("first_publish_year"),
        "isbn": isbns[0] if isbns else None,
        "source": "OpenLibrary",
    }


def normalize_title_key(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def titles_match(a, b, threshold=0.84):
    na, nb = normalize_title_key(a), normalize_title_key(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted([na, nb], key=len)
    # Tolerate one side being "Title: Subtitle" (APIs often add/drop a
    # subtitle) — a clean prefix match is a confident match regardless of
    # the length gap; a substring match elsewhere needs a closer ratio.
    if shorter and longer.startswith(shorter):
        return True
    if shorter and shorter in longer and len(shorter) / len(longer) >= 0.6:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def extract_surnames(authors_str):
    """Crude surname extraction tolerant of '&'/','/'and' separated lists
    and 'Surname, Initials' chunks, for cross-checking against an API's
    author list. Over-matching is fine here; this only gates confidence."""
    if not authors_str:
        return []
    pieces = re.split(r"&|\band\b|,", authors_str)
    surnames = []
    for p in pieces:
        p = p.strip().rstrip(".")
        if not p:
            continue
        words = p.split()
        if not words:
            continue
        candidate = words[0]
        if BARE_SURNAME_RE.match(candidate):
            surnames.append(candidate)
    return surnames


def author_surname_in_candidate(book_authors_str, candidate_authors_list):
    if not book_authors_str or not candidate_authors_list:
        return False
    book_surnames = {s.lower() for s in extract_surnames(book_authors_str)}
    cand_surnames = {full.split()[-1].lower() for full in candidate_authors_list if full and full.split()}
    return bool(book_surnames & cand_surnames)


def first_author_surname_display(authors):
    """Like first_author_key(), but preserves display case for building
    API query strings (rather than returning a normalized dedup key)."""
    if not authors:
        return None
    chunk = authors.split("&")[0].split(" and ")[0].strip().rstrip(",")
    if "," in chunk:
        surname = chunk.split(",", 1)[0].strip()
    else:
        words = chunk.split()
        surname = words[-1] if words else chunk
    return surname or None


def determine_match(book, candidate):
    """Confidence gate for non-ISBN (title/author search) lookups."""
    if not candidate:
        return False
    if not titles_match(book.get("title"), candidate.get("title")):
        return False
    if book.get("authors") and not author_surname_in_candidate(book["authors"], candidate.get("authors")):
        return False
    return True


def format_surname_initial(full_name):
    """'Benjamin J. Evans' -> 'Evans, B. J.'"""
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name.strip()
    surname = parts[-1]
    initials = " ".join(f"{p[0]}." for p in parts[:-1] if p and p[0].isalpha())
    return f"{surname}, {initials}" if initials else surname


def normalize_authors_field(existing, candidate_full_names):
    """Normalize ONLY the bare-surname tokens in `existing` (e.g. "Yonts",
    or "Andrist & Sehr", or "Gough, Bryant & Auburn") into "Surname, F."
    form, using the full names resolved from a confidently-matched API
    record. Tokens that are already a full name ("Vlad Khononov") or
    already in "Surname, F." form ("Enberg, P.") are left untouched —
    they already look correct.

    Returns (new_string_or_None, changed, any_unresolved_bare_token).
    """
    if not existing:
        return None, False, False

    s = re.sub(r",\s*&", " &", existing.strip())
    top_chunks = re.split(r"\s*&\s*|\s+\band\b\s+", s)

    tokens = []  # list of (text, is_bare_surname)
    for chunk in top_chunks:
        chunk = chunk.strip().rstrip(",").strip()
        if not chunk:
            continue
        if SURNAME_INITIALS_RE.match(chunk):
            tokens.append((chunk, False))
            continue
        if "," in chunk:
            for sub in chunk.split(","):
                sub = sub.strip()
                if sub:
                    tokens.append((sub, bool(BARE_SURNAME_RE.match(sub))))
        else:
            tokens.append((chunk, bool(BARE_SURNAME_RE.match(chunk))))

    candidate_surname_map = {}
    for full in candidate_full_names or []:
        parts = full.strip().split()
        if len(parts) >= 2:
            candidate_surname_map[parts[-1].lower()] = full

    changed = False
    unresolved = False
    new_tokens = []
    for text, is_bare in tokens:
        if is_bare:
            full = candidate_surname_map.get(text.lower())
            if full:
                new_tokens.append(format_surname_initial(full))
                changed = True
            else:
                new_tokens.append(text)
                unresolved = True
        else:
            new_tokens.append(text)

    if not changed:
        return None, False, unresolved
    return " & ".join(new_tokens), True, unresolved


def normalize_publisher_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def values_match(field, existing, cand_val):
    if field == "publisher":
        ek, ck = normalize_publisher_key(existing), normalize_publisher_key(cand_val)
        if not ek or not ck:
            return False
        return ek == ck or ek in ck or ck in ek
    if field == "year":
        try:
            return int(existing) == int(cand_val)
        except (TypeError, ValueError):
            return str(existing) == str(cand_val)
    if field == "isbn":
        return normalize_isbn(str(existing)) == normalize_isbn(str(cand_val))
    return str(existing) == str(cand_val)


def apply_review_notes(book, new_notes):
    if not new_notes:
        return
    existing = [n.strip() for n in (book.get("review_note") or "").split(" | ") if n.strip()]
    for n in new_notes:
        if n not in existing:
            existing.append(n)
    book["review_note"] = " | ".join(existing)


def apply_candidate_fields(book, candidate, review_notes):
    changed = False

    for field in ("publisher", "year", "isbn"):
        cand_val = candidate.get(field)
        if cand_val in (None, "", 0):
            continue
        if field == "isbn":
            cand_val = normalize_isbn(str(cand_val))
        if field == "year":
            try:
                cand_val = int(cand_val)
            except (TypeError, ValueError):
                continue

        existing = book.get(field)
        if existing in (None, ""):
            book[field] = cand_val
            changed = True
        elif not values_match(field, existing, cand_val):
            review_notes.append(
                f"{field} conflict: existing='{existing}' vs {candidate.get('source')} "
                f"value='{cand_val}'; kept existing value."
            )

    cand_authors = candidate.get("authors") or []
    if cand_authors:
        existing_authors = book.get("authors")
        if not existing_authors:
            book["authors"] = " & ".join(format_surname_initial(a) for a in cand_authors[:4])
            changed = True
        else:
            normalized, was_changed, _unresolved = normalize_authors_field(existing_authors, cand_authors)
            if was_changed:
                book["authors"] = normalized
                changed = True

    return changed


def enrich_book(book, stats):
    """Enrich a single book dict in place. Fills ONLY empty/missing
    fields on a confident match; on conflict with existing non-empty
    data, keeps the existing value and records a `review_note` instead.
    Never raises — all network access goes through http_get_json(),
    which already swallows failures."""
    review_notes = []
    isbn = (book.get("isbn") or "").strip() or None
    candidate = None
    isbn_keyed = False

    if isbn:
        norm_isbn = normalize_isbn(isbn)
        candidate = google_books_by_isbn(norm_isbn)
        if candidate:
            isbn_keyed = True
        else:
            candidate = openlibrary_by_isbn(norm_isbn)
            if candidate:
                isbn_keyed = True

    if not candidate:
        author_surname = first_author_surname_display(book.get("authors"))
        candidate = google_books_by_title_author(book["title"], author_surname)
        if not candidate:
            candidate = openlibrary_by_title_author(book["title"], author_surname)

    if not candidate:
        stats["enrich_no_hit"] += 1
        return

    if isbn_keyed:
        cand_title = candidate.get("title")
        # Sanity-check against a wildly different title even on an ISBN
        # hit, as a guard against a mistyped/mismatched ISBN in the source.
        confident = titles_match(book["title"], cand_title) if cand_title else True
        if not confident:
            review_notes.append(
                f"ISBN {isbn} lookup via {candidate.get('source')} returned title "
                f"'{cand_title}', which does not match existing title "
                f"'{book['title']}'; left existing data untouched."
            )
            apply_review_notes(book, review_notes)
            stats["enrich_low_confidence"] += 1
            return
    else:
        if not determine_match(book, candidate):
            review_notes.append(
                f"Possible match via {candidate.get('source')} (title='{candidate.get('title')}', "
                f"publisher='{candidate.get('publisher')}', year={candidate.get('year')}) "
                "but title/author match was not confident enough to auto-apply."
            )
            apply_review_notes(book, review_notes)
            stats["enrich_low_confidence"] += 1
            return

    changed = apply_candidate_fields(book, candidate, review_notes)
    if review_notes:
        apply_review_notes(book, review_notes)
    if changed:
        stats["enrich_filled"] += 1


def enrich_books(books):
    stats = {"enrich_filled": 0, "enrich_low_confidence": 0, "enrich_no_hit": 0}
    for book in books:
        enrich_book(book, stats)
    print(
        "gen-books: enrich pass — filled={enrich_filled}, "
        "low-confidence (review_note added)={enrich_low_confidence}, "
        "no API hit={enrich_no_hit}".format(**stats),
        file=sys.stderr,
    )


def iter_scan_files(repo_dir):
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        as_posix = "/" + path.as_posix().strip("/") + "/"
        if any(frag in as_posix for frag in EXCLUDE_FRAGMENTS):
            continue
        yield path


def scan_file(path, slug, stats):
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return []

    stats["files_scanned"] += 1
    found = []
    lines = text.splitlines()

    in_further_reading = False
    heading_level = None

    for line in lines:
        heading_m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_m:
            level = len(heading_m.group(1))
            if FURTHER_READING_HEADING_RE.search(heading_m.group(2)):
                in_further_reading = True
                heading_level = level
            elif in_further_reading and level <= (heading_level or 1):
                in_further_reading = False
            continue

        # Tier 1: explicit ISBN.
        isbn_m = ISBN_RE.search(line)
        if isbn_m:
            isbn = normalize_isbn(isbn_m.group(1))
            prefix_segment = line[: isbn_m.start()]
            title, title_start = extract_title(prefix_segment)
            if title is None:
                # Fall back to a quoted "Title" if no italic marker found.
                qm = re.search(r'"([A-Z][^"]{3,100})"', prefix_segment)
                title = qm.group(1).strip() if qm else None
                title_start = qm.start() if qm else None
            publisher = canonical_publisher(prefix_segment) or canonical_publisher(line)
            year = extract_year(prefix_segment, exclude_span=None) or extract_year(line)
            author = None
            if title_start is not None:
                author = extract_author(prefix_segment[:title_start])
            else:
                author = extract_author(prefix_segment)
            if title is None:
                title = line.strip()[:120]
            found.append(
                {
                    "title": title,
                    "authors": author,
                    "publisher": publisher,
                    "year": year,
                    "isbn": isbn,
                    "url": extract_url(line),
                    "referenced_by": slug,
                    "tier": 1,
                }
            )
            stats["isbn_hits"] += 1
            continue

        # Tier 2: italic/underscore title + known publisher token on the
        # same line. This is a decent filter on its own (the combination
        # is rare outside real book citations); the further-reading
        # heading proximity below is informational only, not a hard gate,
        # since many legitimate citations live in regular prose.
        publisher = canonical_publisher(line)
        if not publisher:
            continue
        title, title_start = extract_title(line)
        if title is None:
            continue
        author = extract_author(line[:title_start])
        year = extract_year(line)
        found.append(
            {
                "title": title,
                "authors": author,
                "publisher": publisher,
                "year": year,
                "isbn": None,
                "url": extract_url(line),
                "referenced_by": slug,
                "tier": 2,
            }
        )
        stats["heuristic_hits"] += 1

    return found


def dedup(records):
    books = {}
    order = []
    title_to_isbn_key = {}

    # Pass 1: ISBN-bearing records win and establish the canonical key for
    # their normalized title, so later non-ISBN mentions of the same book
    # (common — most in-text mentions don't repeat the ISBN) merge into
    # the same entry instead of spawning a duplicate.
    isbn_records = [r for r in records if r["isbn"]]
    other_records = [r for r in records if not r["isbn"]]

    for rec in isbn_records:
        key = ("isbn", rec["isbn"])
        title_to_isbn_key[normalize_key(rec["title"])] = key
        if key not in books:
            books[key] = {
                "title": rec["title"],
                "authors": rec["authors"],
                "publisher": rec["publisher"],
                "year": rec["year"],
                "isbn": rec["isbn"],
                "url": rec["url"],
                "referenced_by": set(),
            }
            order.append(key)
        entry = books[key]
        entry["referenced_by"].add(rec["referenced_by"])
        for field in ("title", "authors", "publisher", "year", "isbn", "url"):
            if not entry.get(field) and rec.get(field):
                entry[field] = rec[field]

    for rec in other_records:
        title_norm = normalize_key(rec["title"])
        key = title_to_isbn_key.get(title_norm) or (
            "title",
            title_norm,
            first_author_key(rec["authors"]),
        )

        if key not in books:
            books[key] = {
                "title": rec["title"],
                "authors": rec["authors"],
                "publisher": rec["publisher"],
                "year": rec["year"],
                "isbn": rec["isbn"],
                "url": rec["url"],
                "referenced_by": set(),
            }
            order.append(key)

        entry = books[key]
        entry["referenced_by"].add(rec["referenced_by"])
        # Fill in any missing fields from this record without clobbering
        # data we already have.
        for field in ("title", "authors", "publisher", "year", "isbn", "url"):
            if not entry.get(field) and rec.get(field):
                entry[field] = rec[field]

    result = []
    for key in order:
        entry = books[key]
        entry["referenced_by"] = sorted(entry["referenced_by"])
        result.append(entry)

    result.sort(key=lambda b: (normalize_key(b["title"]), first_author_key(b["authors"])))
    return result


def yaml_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render_yaml(books):
    lines = [
        "# ============================================================",
        "# MACHINE-GENERATED FILE — do not hand-edit directly.",
        "#",
        "# Generated by scripts/gen-books.py by scanning the allowlisted",
        "# project repos for ISBN mentions and italic-title/publisher",
        "# citations in their Markdown and AsciiDoc sources.",
        "#",
        "# NEEDS HUMAN REVIEW: extraction is heuristic text-mining of",
        "# free-form prose, not a bibliographic parser. Titles, authors,",
        "# publishers, and years may be incomplete, truncated, or wrong.",
        "# Verify before treating this list as authoritative.",
        "#",
        "# Regenerate with:",
        "#   python3 -I scripts/gen-books.py --repos-root <path> --out _data/books.yml",
        "# ============================================================",
        "",
    ]

    if not books:
        lines.append("[]")
        return "\n".join(lines) + "\n"

    for book in books:
        lines.append(f"- title: {yaml_scalar(book['title'])}")
        lines.append(f"  authors: {yaml_scalar(book['authors'])}")
        lines.append(f"  publisher: {yaml_scalar(book['publisher'])}")
        lines.append(f"  year: {yaml_scalar(book['year'])}")
        lines.append(f"  isbn: {yaml_scalar(book['isbn'])}")
        lines.append(f"  url: {yaml_scalar(book['url'])}")
        if book.get("review_note"):
            lines.append(f"  review_note: {yaml_scalar(book['review_note'])}")
        if book["referenced_by"]:
            lines.append("  referenced_by:")
            for slug in book["referenced_by"]:
                lines.append(f"    - {yaml_scalar(slug)}")
        else:
            lines.append("  referenced_by: []")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate _data/books.yml from project repos.")
    parser.add_argument("--repos-root", required=True, help="Directory containing the project repos.")
    parser.add_argument("--out", required=True, help="Path to write the generated YAML.")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help=(
            "After scanning, best-effort enrich each book's missing fields "
            "(publisher/year/isbn/authors) via the public Google Books and "
            "OpenLibrary APIs. Stdlib-only, network-tolerant, never "
            "overwrites a non-empty field; conflicts are recorded as a "
            "review_note instead."
        ),
    )
    args = parser.parse_args()

    repos_root = Path(args.repos_root)
    stats = {"files_scanned": 0, "isbn_hits": 0, "heuristic_hits": 0, "repos_found": 0, "repos_missing": 0}
    records = []

    for slug in ALLOWED_REPOS:
        repo_dir = repos_root / slug
        if not repo_dir.is_dir():
            stats["repos_missing"] += 1
            print(f"warning: repo not found, skipping: {repo_dir}", file=sys.stderr)
            continue
        stats["repos_found"] += 1
        for path in iter_scan_files(repo_dir):
            records.extend(scan_file(path, slug, stats))

    books = dedup(records)

    if args.enrich:
        enrich_books(books)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_yaml(books), encoding="utf-8")

    print(
        "gen-books: repos scanned={repos_found} (missing={repos_missing}), "
        "files scanned={files_scanned}, isbn hits={isbn_hits}, "
        "heuristic hits={heuristic_hits}, raw citations={raw}, "
        "unique books after dedup={unique}".format(
            repos_found=stats["repos_found"],
            repos_missing=stats["repos_missing"],
            files_scanned=stats["files_scanned"],
            isbn_hits=stats["isbn_hits"],
            heuristic_hits=stats["heuristic_hits"],
            raw=len(records),
            unique=len(books),
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
