"""Check really's word counts against Substack's own.

    uv run scripts/wordcounts.py https://www.astralcodexten.com https://simonw.substack.com

For each publication, this takes its latest free posts, reads them the way `really add` would,
and compares the word count with the one Substack reports for the post. A reading time is only
as good as the count behind it, so this is the number to watch when the extraction changes.

Unlike the tests, this uses the network.
"""

from __future__ import annotations

import argparse
import json
import statistics

from rich import box
from rich.console import Console
from rich.table import Table

from really import render
from really.extract import extract
from really.fetch import FetchError, fetch, new_client

TOLERANCE = 0.05


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("publications", nargs="+", metavar="URL", help="a Substack's home page")
    parser.add_argument("-n", "--posts", type=int, default=4, help="posts to check from each")
    args = parser.parse_args()

    console = Console(highlight=False)
    table = Table(box=box.SIMPLE_HEAD, header_style=render.FAINT)
    table.add_column("Post", no_wrap=True, overflow="ellipsis", max_width=56)
    for heading in ("Substack", "really", "Footnotes", "Ratio"):
        table.add_column(heading, justify="right")

    ratios = []
    with new_client() as client, console.status("Reading posts") as status:
        for publication in args.publications:
            home = publication.rstrip("/")
            try:
                listing = fetch(f"{home}/api/v1/archive?sort=new&limit=25", client)
                posts = json.loads(listing.body)
            except (FetchError, ValueError) as e:
                console.print(f"[{render.ERROR}]✗[/] {home}: {e}")
                continue
            free = [post for post in posts if post.get("audience") == "everyone"]
            for post in [post for post in free if post.get("wordcount")][: args.posts]:
                status.update(f"Reading {post['title']}")
                try:
                    article = extract(fetch(post["canonical_url"], client))
                except FetchError as e:
                    console.print(f"[{render.ERROR}]✗[/] {post['title']}: {e}")
                    continue
                ratio = article.words / post["wordcount"]
                ratios.append(ratio)
                off = abs(ratio - 1) > TOLERANCE
                table.add_row(
                    post["title"],
                    f"{post['wordcount']:,}",
                    f"{article.words:,}",
                    f"{article.footnote_words:,}" if article.footnote_words else "",
                    f"[{render.ERROR if off else render.ACCENT}]{ratio:.2f}[/]",
                )

    console.print(table)
    if ratios:
        close = sum(abs(ratio - 1) <= TOLERANCE for ratio in ratios)
        console.print(
            f"{close} of {len(ratios)} within {TOLERANCE:.0%} of Substack's count · "
            f"median ratio {statistics.median(ratios):.3f} · "
            f"furthest off {max(ratios, key=lambda ratio: abs(ratio - 1)):.2f}"
        )


if __name__ == "__main__":
    main()
