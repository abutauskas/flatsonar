"""``python -m flatsea_server.crawler.run --source flathub --limit 100``"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ..db import SessionLocal, init_db
from ..models import CrawlRun, utcnow
from ..settings import settings
from .base import CrawlContext, Source, upsert_candidate

log = logging.getLogger("flatsea.crawler.run")


def build_sources(names: list[str]) -> list[Source]:
    from .codeberg import CodebergSource
    from .flathub import FlathubSource
    from .github import GitHubSource
    from .gitlab import GitLabSource

    table = {
        "flathub": lambda: FlathubSource(),
        "github": lambda: GitHubSource(settings.github_token),
        "gitlab": lambda: GitLabSource(settings.gitlab_token),
        "codeberg": lambda: CodebergSource(settings.codeberg_token),
    }
    if names == ["all"]:
        names = list(table)
    unknown = [n for n in names if n not in table]
    if unknown:
        raise SystemExit(f"unknown source(s): {', '.join(unknown)}; choose from {', '.join(table)} or all")
    return [table[n]() for n in names]


async def crawl(source: Source, ctx: CrawlContext, limit: int | None, commit_every: int = 25) -> CrawlRun:
    run = CrawlRun(source=source.name)
    with SessionLocal() as db:
        db.add(run)
        db.commit()
        pending = 0
        try:
            async for cand in source.discover(ctx, limit):
                try:
                    _, created = upsert_candidate(db, cand)
                except Exception as exc:  # one bad row must not sink the crawl
                    db.rollback()
                    run.errors = [*run.errors, f"{cand.app_id}: {exc}"][-200:]
                    log.warning("upsert %s failed: %s", cand.app_id, exc)
                    continue
                if not cand.is_oss and cand.is_oss is not None:
                    run.skipped += 1
                elif created:
                    run.found += 1
                else:
                    run.updated += 1
                pending += 1
                if pending >= commit_every:
                    db.add(run)
                    db.commit()
                    pending = 0
                    log.info("%s: +%d new, %d updated, %d skipped", source.name, run.found, run.updated, run.skipped)
        finally:
            run.finished_at = utcnow()
            db.add(run)
            db.commit()
    return run


async def amain(args: argparse.Namespace) -> int:
    init_db()
    ctx = CrawlContext(concurrency=args.concurrency)
    try:
        for source in build_sources(args.source):
            log.info("crawling %s (limit=%s)", source.name, args.limit)
            run = await crawl(source, ctx, args.limit)
            log.info(
                "%s done: %d new, %d updated, %d skipped, %d errors",
                source.name, run.found, run.updated, run.skipped, len(run.errors),
            )
    finally:
        await ctx.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Hunt Flatpak apps and write them into the Flatsea database.")
    p.add_argument("--source", nargs="+", default=["flathub"], help="flathub github gitlab codeberg | all")
    p.add_argument("--limit", type=int, default=None, help="max apps (flathub) / repos (forges) per source")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
