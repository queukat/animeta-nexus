#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from loguru import logger

from animeta_nexus import (
    DEFAULT_CHECKPOINT_FILE,
    DEFAULT_PUSH_HEADLESS,
    DEFAULT_PUSH_MAX_RETRIES,
    DEFAULT_PUSH_SLEEP_BETWEEN,
    DEFAULT_PUSH_SLOW_MO_MS,
    DEFAULT_STATE_FILE,
    DEFAULT_TARGET_LANGUAGE,
    ensure_env_loaded,
    push_checkpoint,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push prepared TVDB episode metadata from checkpoint via Playwright."
    )
    parser.add_argument("--checkpoint-file", default=DEFAULT_CHECKPOINT_FILE, help="Path to the pipeline checkpoint JSON.")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Path to the persisted Playwright storage_state.")
    parser.add_argument("--headless", action="store_true", default=DEFAULT_PUSH_HEADLESS, help="Run the browser without UI.")
    parser.add_argument("--slow-mo-ms", type=int, default=DEFAULT_PUSH_SLOW_MO_MS, help="Playwright slow-mo in milliseconds.")
    parser.add_argument("--sleep-between", type=float, default=DEFAULT_PUSH_SLEEP_BETWEEN, help="Pause between saved episodes.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_PUSH_MAX_RETRIES, help="Retries per episode save attempt.")
    parser.add_argument("--max-items", type=int, help="Limit how many checkpoint items to push in one run.")
    parser.add_argument("--target-lang", default=DEFAULT_TARGET_LANGUAGE, help="TVDB target language code, for example eng, spa, fra.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    opts = parser.parse_args()

    if opts.slow_mo_ms < 0:
        parser.error("--slow-mo-ms must be >= 0")
    if opts.sleep_between < 0:
        parser.error("--sleep-between must be >= 0")
    if opts.max_retries < 1:
        parser.error("--max-retries must be >= 1")
    if opts.max_items is not None and opts.max_items < 1:
        parser.error("--max-items must be >= 1")

    return opts


def main() -> None:
    opts = parse_args()
    ensure_env_loaded(("TVDB_PASSWORD",))

    logger.remove()
    logger.add(
        sys.stderr,
        level=str(opts.log_level).upper(),
        format="<level>{level}</level> | {time:YYYY-MM-DD HH:mm:ss} | {message}",
    )

    try:
        asyncio.run(
            push_checkpoint(
                opts.checkpoint_file,
                state_path=opts.state_file,
                headless=opts.headless,
                slow_mo_ms=opts.slow_mo_ms,
                sleep_between=opts.sleep_between,
                max_retries=opts.max_retries,
                max_items=opts.max_items,
                target_lang=opts.target_lang,
            )
        )
    except RuntimeError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    finally:
        if os.path.exists(opts.checkpoint_file):
            logger.info("final checkpoint on exit ({})", opts.checkpoint_file)


if __name__ == "__main__":
    main()
