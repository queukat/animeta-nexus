from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright
from .runtime_environment import env_flag

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CHECKPOINT_FILE = PACKAGE_DIR / "metadata_reconstruction_ledger.json"
DEFAULT_STATE_FILE = PACKAGE_DIR / "tvdb_browser_session_state.json"
DEFAULT_PROCESSED_LOG_FILE = PACKAGE_DIR / "contribution_processed_ledger.txt"
DEFAULT_PUSH_HEADLESS = False
DEFAULT_PUSH_SLOW_MO_MS = 30
DEFAULT_PUSH_SLEEP_BETWEEN = 0.8
DEFAULT_PUSH_MAX_RETRIES = 3
DEFAULT_TARGET_LANGUAGE = os.getenv("TVDB_TARGET_LANGUAGE", os.getenv("TARGET_LANGUAGE", "eng"))
TVDB_EPISODE_NAME_LIMIT = 200
TVDB_EPISODE_OVERVIEW_LIMIT = 950

STATUS_TRANSLATED = "translated"
STATUS_PUSHED = "pushed"
STATUS_LOCKED = "locked"
STATUS_FAILED = "failed"
VALID_STATUSES = {
    STATUS_TRANSLATED,
    STATUS_PUSHED,
    STATUS_LOCKED,
    STATUS_FAILED,
}

LOGIN_URL = "https://thetvdb.com/auth/login?redirect=login"
EDIT_TPL = "https://thetvdb.com/series/-/episodes/{eid}/translate/{lang}/0/single"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_SPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Any, *, limit: Optional[int] = None) -> str:
    if value is None:
        text = ""
    else:
        text = str(value)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_TAG_RE.sub(" ", text)
    text = _CONTROL_RE.sub("", text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    text = text.strip()

    if limit is not None and len(text) > limit:
        text = truncate_text(text, limit)

    return text


def truncate_text(text: str, limit: int) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit].strip()

    suffix = "..."
    target = limit - len(suffix)
    cropped = text[: target + 1].rsplit(" ", 1)[0].strip()
    if len(cropped) < max(1, target // 2):
        cropped = text[:target].strip()
    cropped = cropped.rstrip(".,:;!?- ")
    if not cropped:
        cropped = text[:target].strip().rstrip(".,:;!?- ")
    if len(cropped) > target:
        cropped = cropped[:target].rstrip(".,:;!?- ")
    return f"{cropped}{suffix}"


@dataclass(slots=True)
class PipelineItem:
    episode_id: int
    name: str = ""
    overview: str = ""
    status: str = STATUS_TRANSLATED
    source_lang: str = ""
    source_name: str = ""
    source_overview: str = ""
    translated_at: str = ""
    pushed_at: str = ""
    push_attempts: int = 0
    push_error: str = ""
    last_error_at: str = ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> Optional["PipelineItem"]:
        eid = raw.get("episode_id")
        if not isinstance(eid, int):
            return None

        status = normalize_text(raw.get("status") or STATUS_TRANSLATED, limit=32).lower()
        if status not in VALID_STATUSES:
            status = STATUS_TRANSLATED

        attempts = raw.get("push_attempts", 0)
        try:
            push_attempts = max(0, int(attempts))
        except (TypeError, ValueError):
            push_attempts = 0

        item = cls(
            episode_id=eid,
            name=normalize_text(raw.get("name"), limit=TVDB_EPISODE_NAME_LIMIT),
            overview=normalize_text(raw.get("overview"), limit=4_000),
            status=status,
            source_lang=normalize_text(raw.get("source_lang"), limit=32).lower(),
            source_name=normalize_text(raw.get("source_name"), limit=240),
            source_overview=normalize_text(raw.get("source_overview"), limit=4_000),
            translated_at=normalize_text(raw.get("translated_at"), limit=64),
            pushed_at=normalize_text(raw.get("pushed_at"), limit=64),
            push_attempts=push_attempts,
            push_error=normalize_text(raw.get("push_error"), limit=500),
            last_error_at=normalize_text(raw.get("last_error_at"), limit=64),
        )
        return item

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return {
            key: value
            for key, value in data.items()
            if value not in ("", None) or key in {"episode_id", "name", "overview", "status", "push_attempts"}
        }

    def can_push(self) -> bool:
        return bool(self.name or self.overview) and self.status != STATUS_PUSHED


def merge_pipeline_items(existing: PipelineItem, incoming: PipelineItem) -> PipelineItem:
    merged = PipelineItem(
        episode_id=existing.episode_id,
        name=incoming.name or existing.name,
        overview=incoming.overview or existing.overview,
        status=existing.status,
        source_lang=incoming.source_lang or existing.source_lang,
        source_name=incoming.source_name or existing.source_name,
        source_overview=incoming.source_overview or existing.source_overview,
        translated_at=incoming.translated_at or existing.translated_at,
        pushed_at=incoming.pushed_at or existing.pushed_at,
        push_attempts=max(existing.push_attempts, incoming.push_attempts),
        push_error=incoming.push_error or existing.push_error,
        last_error_at=incoming.last_error_at or existing.last_error_at,
    )

    if existing.status == STATUS_PUSHED and incoming.status != STATUS_PUSHED:
        merged.status = STATUS_PUSHED
    elif existing.status == STATUS_LOCKED and incoming.status == STATUS_TRANSLATED:
        merged.status = STATUS_LOCKED
    else:
        merged.status = incoming.status or existing.status or STATUS_TRANSLATED

    return merged


def load_pipeline_items(path: str | Path) -> List[PipelineItem]:
    target = Path(path)
    if not target.exists():
        logger.info("Checkpoint file {} not found, starting fresh", target)
        return []

    try:
        with target.open(encoding="utf-8") as fp:
            payload = json.load(fp)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load checkpoint file {}: {!r}. Ignoring and starting fresh", target, exc)
        return []

    if isinstance(payload, dict):
        raw_items = payload.get("items", [])
    else:
        raw_items = payload

    if not isinstance(raw_items, list):
        logger.warning("Checkpoint file {} has unexpected format, starting fresh", target)
        return []

    deduped: Dict[int, PipelineItem] = {}
    order: List[int] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = PipelineItem.from_dict(raw)
        if item is None:
            continue
        if item.episode_id in deduped:
            deduped[item.episode_id] = merge_pipeline_items(deduped[item.episode_id], item)
            continue
        deduped[item.episode_id] = item
        order.append(item.episode_id)

    items = [deduped[eid] for eid in order]
    logger.info("Loaded {} existing translations from {}", len(items), target)
    return items


def save_pipeline_items(path: str | Path, items: Iterable[PipelineItem]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.to_dict() for item in sorted(items, key=lambda current: current.episode_id)]

    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=target.parent, suffix=".tmp") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        tmp_path = fp.name

    os.replace(tmp_path, target)
    logger.info("Saved {} translations to {}", len(payload), target)


def append_processed_episode(path: str | Path, episode_id: int, status: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fp:
        fp.write(f"{episode_id}\t{status}\t{_utc_now()}\n")


def load_processed_episode_ids(path: str | Path) -> set[int]:
    target = Path(path)
    if not target.exists():
        return set()

    ids: set[int] = set()
    with target.open(encoding="utf-8") as fp:
        for line in fp:
            match = re.search(r"\d+", line)
            if match:
                ids.add(int(match.group()))
    return ids


async def is_login_page(page) -> bool:
    email_label = page.get_by_label("Email Address")
    login_button = page.get_by_role("button", name="Login")
    return bool(await email_label.count() and await login_button.count())


async def login(page, *, user: str, password: str, state_path: str | Path) -> None:
    if not user or not password:
        raise RuntimeError("TVDB_USER or TVDB_USERNAME and TVDB_PASSWORD are not set in environment")

    page.set_default_timeout(60_000)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if not await is_login_page(page):
        logger.info("TVDB browser session is already authenticated")
        await page.context.storage_state(path=Path(state_path).as_posix())
        return

    logger.info("TVDB login form detected, authenticating")
    await page.get_by_label("Email Address").fill(user)
    await page.get_by_label("Password").fill(password)
    await page.get_by_role("button", name="Login").click()

    try:
        await page.wait_for_url(
            lambda url: ("/auth/login" not in url) and ("/index.php/auth/login" not in url),
            timeout=60_000,
        )
    except PWTimeout:
        pass

    await page.wait_for_timeout(500)
    if await is_login_page(page):
        err = page.locator(".flash-message .alert.alert-danger, .alert.alert-danger")
        if await err.count():
            raise RuntimeError(f"TVDB login failed: {(await err.first.inner_text()).strip()}")
        raise RuntimeError("TVDB login did not complete; possible captcha, 2FA, or bot protection")

    await page.context.storage_state(path=Path(state_path).as_posix())
    logger.info("TVDB browser session stored in {}", state_path)


async def is_episode_locked(page) -> bool:
    name_input = page.locator('input[name="episode_name"]')
    overview_area = page.locator('textarea[name="episode_overview"]')
    save_button = page.locator("form.episode-translate-form button[type='submit']")

    locked_by_fields = False

    if await name_input.count():
        readonly_attr = await name_input.get_attribute("readonly")
        if readonly_attr is not None or await name_input.is_disabled():
            locked_by_fields = True

    if await overview_area.count():
        readonly_attr = await overview_area.get_attribute("readonly")
        if readonly_attr is not None or await overview_area.is_disabled():
            locked_by_fields = True

    if locked_by_fields:
        return True

    return await save_button.count() == 0


async def wait_for_save_flash(page, timeout_ms: int = 10_000) -> Tuple[Optional[bool], Optional[str]]:
    success = page.locator(
        ".flash-message .alert.alert-success",
        has_text="This translation has been saved.",
    ).first
    error = page.locator(".flash-message .alert.alert-danger").first

    success_task = asyncio.create_task(success.wait_for(state="attached"))
    error_task = asyncio.create_task(error.wait_for(state="attached"))

    done, pending = await asyncio.wait(
        {success_task, error_task},
        timeout=timeout_ms / 1000,
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    if not done:
        return None, None

    if success_task in done and success_task.exception() is None:
        return True, None

    if error_task in done and error_task.exception() is None:
        try:
            return False, (await error.first.inner_text()).strip() or "Unknown error flash"
        except Exception:  # noqa: BLE001
            return False, "Unknown error flash"

    finished = next(iter(done))
    exc = finished.exception()
    return None, str(exc) if exc else None


async def confirm_translation_saved(page, expected_name: str, timeout_ms: int = 10_000) -> bool:
    try:
        if "/translate/" in page.url:
            try:
                await page.wait_for_url("**/episodes/**", timeout=timeout_ms)
            except PWTimeout:
                pass

        title = page.locator("h1.translated_title")
        await title.wait_for(timeout=timeout_ms)
        actual = normalize_text(await title.inner_text(), limit=TVDB_EPISODE_NAME_LIMIT)
        return bool(expected_name and actual == normalize_text(expected_name, limit=TVDB_EPISODE_NAME_LIMIT))
    except Exception:  # noqa: BLE001
        return False


async def save_translation(
    page,
    item: PipelineItem,
    *,
    user: str,
    password: str,
    state_path: str | Path,
    target_lang: str = DEFAULT_TARGET_LANGUAGE,
    max_retries: int = DEFAULT_PUSH_MAX_RETRIES,
) -> Tuple[str, str]:
    url = EDIT_TPL.format(eid=item.episode_id, lang=target_lang)
    name = truncate_text(item.name, TVDB_EPISODE_NAME_LIMIT) if item.name else ""
    overview = truncate_text(item.overview, TVDB_EPISODE_OVERVIEW_LIMIT) if item.overview else ""

    if item.name and item.name != name:
        logger.warning(
            "Episode {}: title truncated from {} to {} characters to satisfy TVDB limit",
            item.episode_id,
            len(item.name),
            len(name),
        )
        item.name = name
    if item.overview and item.overview != overview:
        logger.info(
            "Episode {}: overview truncated from {} to {} characters to satisfy TVDB limit",
            item.episode_id,
            len(item.overview),
            len(overview),
        )
        item.overview = overview

    if not name and not overview:
        return STATUS_FAILED, "checkpoint item has no pushable text"

    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded")

            if await is_login_page(page):
                logger.warning("Episode {} redirected to login, refreshing session", item.episode_id)
                await login(page, user=user, password=password, state_path=state_path)
                await page.goto(url, wait_until="domcontentloaded")

            if await is_episode_locked(page):
                return STATUS_LOCKED, "episode translation form is locked"

            await page.fill('input[name="episode_name"]', name)
            await page.fill('textarea[name="episode_overview"]', overview)

            save_btn = page.locator("form.episode-translate-form button[type='submit']")
            flash_wait_task = asyncio.create_task(wait_for_save_flash(page, timeout_ms=10_000))
            await save_btn.click()

            flash_ok, flash_msg = await flash_wait_task
            if flash_ok is True:
                return STATUS_PUSHED, ""

            if flash_ok is None and await confirm_translation_saved(page, name):
                return STATUS_PUSHED, ""

            if attempt == max_retries:
                return STATUS_FAILED, normalize_text(flash_msg or "could not confirm save", limit=500)

            await asyncio.sleep(2)
        except PWTimeout:
            if attempt == max_retries:
                return STATUS_FAILED, "playwright timeout while saving translation"
            await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            if attempt == max_retries:
                return STATUS_FAILED, normalize_text(str(exc), limit=500)
            await asyncio.sleep(2)

    return STATUS_FAILED, "unknown push failure"


async def push_checkpoint(
    checkpoint_path: str | Path,
    *,
    state_path: str | Path = DEFAULT_STATE_FILE,
    processed_log_path: str | Path = DEFAULT_PROCESSED_LOG_FILE,
    headless: bool = DEFAULT_PUSH_HEADLESS,
    slow_mo_ms: int = DEFAULT_PUSH_SLOW_MO_MS,
    sleep_between: float = DEFAULT_PUSH_SLEEP_BETWEEN,
    max_retries: int = DEFAULT_PUSH_MAX_RETRIES,
    max_items: Optional[int] = None,
    target_lang: str = DEFAULT_TARGET_LANGUAGE,
    user: Optional[str] = None,
    password: Optional[str] = None,
    delete_pushed_from_checkpoint: Optional[bool] = None,
) -> Dict[str, int]:
    items = load_pipeline_items(checkpoint_path)
    if not items:
        logger.info("Checkpoint {} is empty, nothing to push", checkpoint_path)
        return {"queued": 0, "pushed": 0, "locked": 0, "failed": 0}

    if delete_pushed_from_checkpoint is None:
        delete_pushed_from_checkpoint = env_flag("TVDB_DELETE_PUSHED_FROM_CHECKPOINT", default=False)

    processed_ids = load_processed_episode_ids(processed_log_path)

    if delete_pushed_from_checkpoint:
        original_count = len(items)
        removed_items = [item for item in items if item.status == STATUS_PUSHED]
        for item in removed_items:
            if item.episode_id not in processed_ids:
                append_processed_episode(processed_log_path, item.episode_id, STATUS_PUSHED)
                processed_ids.add(item.episode_id)
        items = [item for item in items if item.status != STATUS_PUSHED]
        removed_existing_pushed = original_count - len(items)
        if removed_existing_pushed:
            save_pipeline_items(checkpoint_path, items)
            logger.info(
                "Push preflight cleanup: removed {} already-pushed records from {}",
                removed_existing_pushed,
                checkpoint_path,
            )

    status_counts = Counter(item.status for item in items)
    missing_text = sum(1 for item in items if not (item.name or item.overview))
    queue = [item for item in items if item.can_push() and item.status != STATUS_LOCKED]
    if max_items is not None:
        queue = queue[: max(0, max_items)]

    logger.info(
        "Push preflight: total_items={} queued={} status_counts={} missing_text={} delete_pushed_from_checkpoint={} processed_log={}",
        len(items),
        len(queue),
        dict(status_counts),
        missing_text,
        delete_pushed_from_checkpoint,
        Path(processed_log_path),
    )

    if not queue:
        logger.info("No translated items require push")
        return {"queued": 0, "pushed": 0, "locked": 0, "failed": 0}

    user = user or os.getenv("TVDB_USER") or os.getenv("TVDB_USERNAME") or ""
    password = password or os.getenv("TVDB_PASSWORD") or ""
    if not user or not password:
        raise RuntimeError("TVDB_USER or TVDB_USERNAME and TVDB_PASSWORD must be set for push mode")

    by_id = {item.episode_id: item for item in items}
    summary = {"queued": len(queue), "pushed": 0, "locked": 0, "failed": 0}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context_kwargs: Dict[str, Any] = {"locale": "en-US"}
        state_file = Path(state_path)
        if state_file.exists():
            context_kwargs["storage_state"] = state_file.as_posix()

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        await login(page, user=user, password=password, state_path=state_file)

        for index, item in enumerate(queue, 1):
            current = by_id[item.episode_id]
            logger.info("Push {}/{} episode {}", index, len(queue), current.episode_id)

            status, error = await save_translation(
                page,
                current,
                user=user,
                password=password,
                state_path=state_file,
                target_lang=target_lang,
                max_retries=max_retries,
            )

            current.push_attempts += 1
            current.push_error = normalize_text(error, limit=500)

            if status == STATUS_PUSHED:
                if current.episode_id not in processed_ids:
                    append_processed_episode(processed_log_path, current.episode_id, STATUS_PUSHED)
                    processed_ids.add(current.episode_id)
                if delete_pushed_from_checkpoint:
                    del by_id[current.episode_id]
                else:
                    current.status = STATUS_PUSHED
                    current.pushed_at = _utc_now()
                    current.push_error = ""
                summary["pushed"] += 1
            elif status == STATUS_LOCKED:
                current.status = STATUS_LOCKED
                current.last_error_at = _utc_now()
                if current.episode_id not in processed_ids:
                    append_processed_episode(processed_log_path, current.episode_id, STATUS_LOCKED)
                    processed_ids.add(current.episode_id)
                summary["locked"] += 1
            else:
                current.status = STATUS_FAILED
                current.last_error_at = _utc_now()
                summary["failed"] += 1

            save_pipeline_items(checkpoint_path, by_id.values())
            await asyncio.sleep(5 if index % 20 == 0 else sleep_between)

        await browser.close()

    logger.info("Push summary: {}", summary)
    return summary
