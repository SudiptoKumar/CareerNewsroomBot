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
    now = datetime.now(BD_TZ)
    session = config.scheduler_window(now.hour)
    if not manual:
        if not session:
            logger.info("SCHEDULER GUARD | outside session window | local=%s", now.isoformat())
            return 0
        if not state.schedule_allowed(session, now, manual=False):
            logger.info("SCHEDULER GUARD | %s session already completed | date=%s", session, now.date())
            return 0

    logger.info("=== %s ===", config.pipeline_version)
    logger.info("RUN | local=%s | manual=%s | dry_run=%s", now.isoformat(), manual, dry_run)
    logger.info("RULES | max_post_age_days=%d (today + previous %d calendar days) | experience<=%d | age=%d-%d | targets private=%d govt=%d internship=%d extras<=%d max=%d",
                config.max_post_age_days, config.max_post_age_days - 1, config.max_experience_years, config.min_age, config.max_age,
                config.private_target, config.government_target, config.internship_target, config.flexible_extras, config.hard_max_posts)

    http = HttpClient(config)
    pipe = Pipeline(config, state, http)
    expired = pipe.expire()
    logger.info("EXPIRY SWEEP | updated=%d", expired)

    discovered = pipe.discover()
    logger.info("FUNNEL | discovered=%d", len(discovered))
    enriched = pipe.enrich(discovered)
    logger.info("FUNNEL | detail_attempted=%d | detail_valid=%d | detail_failed=%d",
                len(enriched), sum(x.extraction_status == "detail_validated" for x in enriched),
                sum(x.extraction_status == "detail_fetch_failed" for x in enriched))

    eligible, gate_counts = pipe.filter(enriched, now.date())
    logger.info("HARD GATE | eligible=%d | %s", len(eligible), " | ".join(f"{k}={v}" for k, v in sorted(gate_counts.items())))
    logger.info("AI INPUT | count=%d | all have verified published_date + active deadline", len(eligible))

    ranked = pipe.ai_rank(eligible)
    selected = pipe.select(ranked)
    logger.info("SELECTION | selected=%d | private=%d | government=%d | internships=%d",
                len(selected),
                sum(not x.is_government and not x.is_internship for x in selected),
                sum(x.is_government for x in selected),
                sum((not x.is_government) and x.is_internship for x in selected))

    published = pipe.publish(selected, dry_run=dry_run)
    logger.info("FINISHED | published=%d | ai_available=%s | ai_reason=%s", published, pipe.ai.available, pipe.ai.reason)
    state.data["last_run"] = now.isoformat(); state.data["pipeline_version"] = config.pipeline_version
    state.data.setdefault("run_history", []).append({
        "started_at": now.isoformat(), "manual": manual, "dry_run": dry_run,
        "discovered": len(discovered), "detail_valid": sum(x.extraction_status == "detail_validated" for x in enriched),
        "eligible_for_ai": len(eligible), "selected": len(selected), "published": published,
        "gate_counts": gate_counts,
    })
    state.prune(); state.save()
    return 0


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
