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
import re
import sys
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
