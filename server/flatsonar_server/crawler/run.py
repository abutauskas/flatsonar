"""``python -m flatsonar_server.crawler.run --source flathub --limit 100``"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta

from sqlalchemy import select, update

from ..db import SessionLocal, init_db
from ..models import CrawlRun, utcnow
from ..settings import settings
from .base import CrawlContext, Skipped, Source, upsert_candidate

log = logging.getLogger("flatsonar.crawler.run")

# How long an unfinished CrawlRun is trusted as "still genuinely running" before
# it is treated as abandoned. Real per-source crawls finish in minutes; this is
# generous headroom, not a target. A row can outlive its process (killed rather
# than let through its own `finally`, a crash, a host reboot mid-run) and sit
# with finished_at=NULL forever otherwise, permanently blocking that source.
STALE_RUN_AFTER = timedelta(hours=2)


def _already_running(db, source_name: str) -> CrawlRun | None:
    """An unfinished, still-plausibly-live CrawlRun for this source, if any."""
    cutoff = utcnow() - STALE_RUN_AFTER
    return db.scalar(
        select(CrawlRun)
        .where(CrawlRun.source == source_name, CrawlRun.finished_at.is_(None), CrawlRun.started_at >= cutoff)
        .order_by(CrawlRun.started_at.desc())
        .limit(1)
    )


def build_sources(names: list[str]) -> list[Source]:
    from .codeberg import CodebergSource
    from .flathub import FlathubSource
    from .github import GitHubSource
    from .gitlab import GitLabSource

    gitlab_instances = ("https://gitlab.com", *(u.strip() for u in settings.gitlab_instances.split(",") if u.strip()))
    table = {
        "flathub": lambda: FlathubSource(),
        "github": lambda: GitHubSource(settings.github_token),
        "gitlab": lambda: GitLabSource(settings.gitlab_token, base_urls=gitlab_instances),
        "codeberg": lambda: CodebergSource(settings.codeberg_token),
    }
    if names == ["all"]:
        names = list(table)
    unknown = [n for n in names if n not in table]
    if unknown:
        raise SystemExit(f"unknown source(s): {', '.join(unknown)}; choose from {', '.join(table)} or all")
    return [table[n]() for n in names]


def _safe_commit(db, run: CrawlRun, what: str) -> bool:
    """Commit, or roll back and keep going. A single crawl can touch thousands of
    apps from hundreds of different projects; one row that fails to *commit* (as
    opposed to one that fails to even build, which ``upsert_candidate`` callers
    already handle) must not be allowed to take the rest of the run down with it."""
    try:
        db.add(run)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        run.errors = [*run.errors, f"{what}: {exc}"][-200:]
        log.error("commit failed (%s), rolled back and continuing: %s", what, exc)
        return False


async def crawl(source: Source, ctx: CrawlContext, limit: int | None, commit_every: int = 25) -> CrawlRun:
    with SessionLocal() as db:
        existing = _already_running(db, source.name)
        if existing is not None:
            log.warning("%s: a crawl started %s is still marked running; skipping this one "
                        "rather than duplicate its work", source.name, existing.started_at)
            return existing
        run = CrawlRun(source=source.name)
        db.add(run)
        db.commit()
        pending = 0
        try:
            async for cand in source.discover(ctx, limit):
                try:
                    _, created = upsert_candidate(db, cand)
                except Skipped as why:
                    log.info("skipped %s", why)
                    run.skipped += 1
                    pending += 1
                    continue
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
                    _safe_commit(db, run, f"batch ending at {cand.app_id}")
                    pending = 0
                    log.info("%s: +%d new, %d updated, %d skipped", source.name, run.found, run.updated, run.skipped)
        finally:
            run.finished_at = utcnow()
            if not _safe_commit(db, run, "final"):
                # The final commit can fail on a leftover pending candidate from this
                # same batch (two forks landing on the same app_id in one flush,
                # tripping the unique constraint) - _safe_commit's rollback discards
                # that together with the finished_at we just set. That must not cost
                # us marking the run finished, or it looks "running" forever and
                # blocks a future crawl for this source (see _already_running).
                db.execute(update(CrawlRun).where(CrawlRun.id == run.id).values(
                    finished_at=run.finished_at, found=run.found, updated=run.updated,
                    skipped=run.skipped, errors=run.errors,
                ))
                db.commit()
    return run


async def _crawl_one(source: Source, ctx: CrawlContext, limit: int | None) -> str | None:
    """Runs one source to completion and logs its result. Returns its name on
    failure, None on success, so :func:`amain` can gather every source without one
    source's exception taking the others down with it (belt and braces on top of
    :func:`crawl`'s own per-candidate recovery: a genuinely bad day for one forge -
    it's down, a bug we have not hit yet - must not stop the rest)."""
    log.info("crawling %s (limit=%s)", source.name, limit)
    try:
        run = await crawl(source, ctx, limit)
    except Exception:
        log.exception("%s crawl aborted", source.name)
        return source.name
    log.info(
        "%s done: %d new, %d updated, %d skipped, %d errors",
        source.name, run.found, run.updated, run.skipped, len(run.errors),
    )
    return None


async def amain(args: argparse.Namespace) -> int:
    init_db()
    ctx = CrawlContext(concurrency=args.concurrency)
    try:
        # Each source hits a different host and already paces itself (per-host rate
        # limiting, its own bounded worker pool), so running them side by side rather
        # than one after another means one source's rate-limit wait no longer stalls
        # the other three - they share the same concurrency budget but no longer sit
        # fully idle while e.g. GitHub sleeps off a search rate limit.
        results = await asyncio.gather(*(_crawl_one(s, ctx, args.limit) for s in build_sources(args.source)))
    finally:
        await ctx.close()
    failed = [name for name in results if name]
    if failed:
        log.error("source(s) aborted: %s", ", ".join(failed))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Hunt Flatpak apps and write them into the Flatsonar database.")
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
