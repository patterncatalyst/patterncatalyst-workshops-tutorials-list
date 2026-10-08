# PatternCatalyst Workshops and Tutorials

A hub that indexes the PatternCatalyst family of workshops, tutorials, and
reference builds, with a curated reading list and blog links.

**Live site:**
<https://patterncatalyst.github.io/patterncatalyst-workshops-tutorials-list/>

## What's here

- **Workshops** (home): a card grid of our project sites, grouped by theme
  (Cloud-Native & Kubernetes, DDD & Integration, Performance & Optimization,
  Systems Programming, and more).
- **Our Blogs:** posts we've written from this material, published on the
  [Pattern Catalyst Blog](https://patterncatalyst.github.io/patterncatalyst-blog/).
- **Blog Links:** curated external blogs and authors we reference.
- **Books:** books cited across the projects, grouped by topic, each linked to
  the publisher.

## How it's built

A [Jekyll](https://jekyllrb.com/) site in the PatternCatalyst house style
(Red Hat fonts, amber accent), deployed to GitHub Pages by the workflow in
`.github/workflows/pages.yml`. Content is data-driven from `_data/`:

| File | Drives |
|------|--------|
| `_data/sites.yml` | the home-page card grid of project sites |
| `_data/books.yml` + `_data/book_categories.yml` | the Books page (catalog + topical grouping) |
| `_data/blog_links.yml` | the Blog Links page |
| `_data/blogs.yml` | the Our Blogs page |

`scripts/gen-books.py` compiles the book catalog from citations across the
project repos (stdlib-only; run with `python3 -I`).

## Run locally

```bash
bundle install
bundle exec jekyll serve --baseurl ""
# http://localhost:4000/
```

## Contributing

See [`CLAUDE.md`](CLAUDE.md) for the conventions: how to add a site, book, or
link, the table-based layout, and the writing-voice rules.
