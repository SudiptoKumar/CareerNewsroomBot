from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from core.config import Config, BD_TZ
from core.http import HttpClient
from core.pipeline import Pipeline
from core.state import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("career-news-v2")
ROOT = Path(__file__).resolve().parent


def run(*, manual: bool, dry_run: bool) -> int:
    config = Config()
    state = StateStore(config, ROOT)
    started_at = datetime.now(BD_TZ)
    session = config.scheduler_window(started_at.hour)
    claimed = False

    if not manual:
        if not session:
            logger.info("SCHEDULER GUARD | outside session window | local=%s", started_at.isoformat())
            return 0
        if not state.schedule_allowed(session, started_at, manual=False):
            logger.info("SCHEDULER GUARD | %s session already running/completed | date=%s", session, started_at.date())
            return 0
        claimed = True

    logger.info("=== %s ===", config.pipeline_version)
    logger.info("RUN | local=%s | manual=%s | dry_run=%s | session=%s", started_at.isoformat(), manual, dry_run, session or "manual")
    logger.info(
        "RULES | max_post_age_days=%d => today + previous %d calendar days | experience<=%d | age=%d-%d | targets private=%d govt=%d internship=%d extras<=%d max=%d",
        config.max_post_age_days, max(0, config.max_post_age_days - 1), config.max_experience_years,
        config.min_age, config.max_age, config.private_target, config.government_target,
        config.internship_target, config.flexible_extras, config.hard_max_posts,
    )

    try:
        http = HttpClient(config)
        pipe = Pipeline(config, state, http)

        expired = pipe.expire()
        logger.info("EXPIRY SWEEP | updated=%d", expired)

        discovered = pipe.discover()
        by_source = {}
        for job in discovered:
            by_source[job.source] = by_source.get(job.source, 0) + 1
        logger.info("DISCOVERY COMPLETE | total=%d | %s", len(discovered), " | ".join(f"{k}={v}" for k, v in sorted(by_source.items())))

        enriched = pipe.enrich(discovered)
        detail_valid = sum(x.extraction_status in {"detail_validated", "api_validated"} for x in enriched)
        detail_failed = len(enriched) - detail_valid
        logger.info("DETAIL COMPLETE | attempted=%d | verified=%d | failed=%d", len(enriched), detail_valid, detail_failed)

        eligible, gate_counts = pipe.filter(enriched, started_at.date())
        logger.info("HARD GATE COMPLETE | ai_eligible=%d | %s", len(eligible), " | ".join(f"{k}={v}" for k, v in sorted(gate_counts.items())))
        logger.info("AI INPUT INVARIANT | count=%d | all verified published_date + active deadline + deterministic eligibility", len(eligible))

        ranked = pipe.ai_rank(eligible)
        selected = pipe.select(ranked)
        logger.info(
            "SELECTION | selected=%d | private=%d | government=%d | internships=%d",
            len(selected),
            sum(not x.is_government and not x.is_internship for x in selected),
            sum(x.is_government for x in selected),
            sum((not x.is_government) and x.is_internship for x in selected),
        )

        published = pipe.publish(selected, dry_run=dry_run)
        logger.info("FINISHED | published=%d | ai_available=%s | ai_reason=%s", published, pipe.ai.available, pipe.ai.reason)

        state.data["last_run"] = started_at.isoformat()
        state.data["pipeline_version"] = config.pipeline_version
        state.data.setdefault("run_history", []).append({
            "started_at": started_at.isoformat(),
            "manual": manual,
            "dry_run": dry_run,
            "discovered": len(discovered),
            "detail_attempted": len(enriched),
            "detail_valid": detail_valid,
            "eligible_for_ai": len(eligible),
            "selected": len(selected),
            "published": published,
            "gate_counts": gate_counts,
            "ai_available": pipe.ai.available,
            "ai_reason": pipe.ai.reason,
        })
        state.prune()
        state.save()
        if claimed and session:
            state.complete_schedule(session, datetime.now(BD_TZ))
        return 0
    except Exception as exc:
        logger.exception("RUN FAILED | error=%s", exc)
        if claimed and session:
            try:
                state.release_schedule(session, datetime.now(BD_TZ), str(exc))
            except Exception:
                logger.exception("SCHEDULER GUARD | failed to release claimed session")
        return 1


def self_test() -> int:
    from tests.test_v2 import run_self_test
    run_self_test()
    print("SELF-TEST: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual", action="store_true", help="Bypass scheduler guard")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except Telegram publishing")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return run(manual=args.manual, dry_run=args.dry_run)

if __name__ == "__main__":
    raise SystemExit(main())
