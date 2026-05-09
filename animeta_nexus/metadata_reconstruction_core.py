#!/usr/bin/env python3
"""
metadata_reconstruction_core.py

CLI utility for batch reconstruction of localized TVDB episode metadata
("name" + "overview") through TVDB API source signals and an LLM provider.

Operational notes
- The script reads metadata through TVDB API v4, stores a local checkpoint,
  and can optionally push reviewed records back through a Playwright workflow.

Checkpoint JSON shape
[
  {
    "episode_id": 11544486,
    "name": "...",
    "overview": "..."
  },
  ...
]

Main modes
- --season-id <id>   : process one TVDB season.
- --series-id <id>   : process all episodes for one TVDB series.
- --all-anime        : discover anime series and process their episodes.

Year filter for --all-anime
- --year <YYYY>      : keep series whose firstAired year matches.

Concurrency
- --concurrent N     : cap TVDB + provider concurrency.

Source languages for episode names and overviews
- Default order: originalLanguage -> eng.
- If original-language payloads are weak, English and base episode fields are used.
- Override the order with --source-langs.

Checkpoint
- metadata_reconstruction_ledger.json stores generated metadata and push-stage status so long
  runs can resume safely.

Extra controls
- --include-episode-id <id> can be repeated to force individual episode IDs
  into a run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import backoff  # type: ignore
import httpx  # type: ignore
import regex  # type: ignore
from httpx import HTTPStatusError
from loguru import logger
from openai import AsyncOpenAI  # type: ignore
from animeta_nexus import (
    DEFAULT_CHECKPOINT_FILE,
    DEFAULT_PUSH_HEADLESS,
    DEFAULT_PUSH_MAX_RETRIES,
    DEFAULT_PUSH_SLEEP_BETWEEN,
    DEFAULT_PUSH_SLOW_MO_MS,
    DEFAULT_PROCESSED_LOG_FILE,
    DEFAULT_STATE_FILE,
    DEFAULT_TARGET_LANGUAGE,
    PipelineItem,
    ensure_env_loaded,
    load_pipeline_items,
    merge_pipeline_items,
    normalize_text,
    push_checkpoint,
    save_pipeline_items,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TVDB_BASE_URL = "https://api4.thetvdb.com/v4"
DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
DEFAULT_TARGET_LANGUAGE_NAME = os.getenv("TARGET_LANGUAGE_NAME", "English")

ANIME_GENRE_NAME = "Anime"
TRANSLATIONS_CHECKPOINT_FILE = DEFAULT_CHECKPOINT_FILE
DEFAULT_TRANSLATION_BATCH_SIZE = 8
JAPANESE_ORIGINAL_LANGUAGE_CODES = frozenset({"ja", "jp", "jpn"})
LANGUAGE_NAMES = {
    "eng": "English",
    "en": "English",
    "spa": "Spanish",
    "es": "Spanish",
    "fra": "French",
    "fre": "French",
    "fr": "French",
    "deu": "German",
    "ger": "German",
    "de": "German",
    "ita": "Italian",
    "it": "Italian",
    "por": "Portuguese",
    "pt": "Portuguese",
}

# ---------------------------------------------------------------------------
# Placeholder regexes
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(
    r"""
    ^
    (?:
        TBA|TBD|TBC|N/A|TWA
      | To\s*Be\s*(?:Announced|Determined|Confirmed)
      | Unknown\s*Title|Untitled
      | ---+
      | 未定|待定|暂无简介
    )
    \.?
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

GENERIC_EP_TITLE_RE = re.compile(
    r"""
    ^
    (?:
        (?:
            (?:episode|ep\.?|e)\s*[-\s]*\d{1,3}
        )
        |
        (?:第?\s*\d+\s*(?:話|回))
        |
        (?:第?\s*\d+\s*(?:集|话|話))
        |
        (?:[Ee]?\d{1,3})
    )
    \s*[\.\-–—:]*\s*$
    """,
    re.IGNORECASE | re.VERBOSE | re.UNICODE,
)


def _is_generic_episode_title(text: str) -> bool:
    if not text:
        return False
    return bool(GENERIC_EP_TITLE_RE.match(text.strip()))


def _looks_empty_or_punct(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    core = regex.sub(r"[\p{P}\p{S}\s]+", "", t)
    return len(core) < 2


def _is_placeholder(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if _looks_empty_or_punct(t):
        return True
    return bool(PLACEHOLDER_RE.match(t) or _is_generic_episode_title(t))


def _is_japanese_original_language(value: Any) -> bool:
    if value is None:
        return False
    lang = normalize_text(str(value), limit=16).lower()
    return lang in JAPANESE_ORIGINAL_LANGUAGE_CODES


def _target_language_name(code: str, explicit_name: Optional[str] = None) -> str:
    if explicit_name:
        return normalize_text(explicit_name, limit=64)
    normalized = _norm_lang(code)
    return LANGUAGE_NAMES.get(normalized, normalized or "the target language")


# ---------------------------------------------------------------------------
# OpenAI helper
# ---------------------------------------------------------------------------

def _style_rules(target_language_name: str) -> str:
    return f"""
You are a metadata reconstruction engine for TV and streaming databases.
Reconstruct episode titles and descriptions into natural {target_language_name}.

STYLE
- Write fluent {target_language_name} suitable for media-library and TV database metadata.
- Prefer concise, idiomatic phrasing over literal word-for-word rendering.
- Keep the tone of a professional synopsis: clear, neutral, and readable.
- Episode titles should sound like real localized titles, not mechanical calques.

CONTEXT-AWARE RECONSTRUCTION
- Preserve the original meaning exactly; do not omit key plot points and do not add facts.
- Use the language hint only as a hint; if it conflicts with the text, trust the text.
- Source titles and descriptions may be in different languages; evaluate each field independently.
- Track recurring title patterns, numbering, character names, organizations, and fictional places.
- Keep repeated naming patterns stable across neighboring episodes in the same batch.

TITLE SOURCE STRICTNESS
- If the source title is missing, empty, or not provided for an episode, output an empty title "".
- Never infer or invent an episode title from the description/overview.

SANITIZATION
- Use standard punctuation and orthography for {target_language_name}.
- Do not output emoji or decorative symbols.
"""


def _placeholder_rules(target_language_name: str) -> str:
    return f"""
DO NOT INVENT / PLACEHOLDERS
- If the source title and/or description are empty, missing, or a placeholder,
  set that output field to an empty string "".
- Treat as placeholders (case-insensitive, optional dots/spaces):
  EN: TBA, TBD, TBC, N/A, TWA, "To Be Announced/Determined/Confirmed",
      "Unknown Title", "Untitled", sequences of only dashes.
  CJK: 未定, 待定, 暂无简介.
- Also treat as placeholder any string shorter than 2 non-punctuation characters or
  consisting only of punctuation/whitespace.
- Never translate placeholders into {target_language_name} stubs.
"""


def build_system_prompt_single(target_language_name: str) -> str:
    return (
        _style_rules(target_language_name)
        + f"""

INPUT
- You receive ONE JSON object as user content with fields:
  {{
    "title_original": "<string>",
    "description_original": "<string>",
    "language": "<hint, e.g. 'jpn', 'eng', 'zho'>"
  }}

FINAL QUALITY CHECK
- Before returning JSON, silently check that each title and description:
  1) is grammatical {target_language_name};
  2) does not contain literal calques that change the meaning;
  3) keeps the same factual content as the source;
  4) is concise enough for database-style metadata.
- Fix such issues before output.

OUTPUT FORMAT
- Return ONLY valid JSON with exactly these keys:
  {{"title": "<string>", "description": "<string>"}}
"""
        + _placeholder_rules(target_language_name)
    )


def build_system_prompt_batch(target_language_name: str) -> str:
    return (
        _style_rules(target_language_name)
        + f"""

INPUT
- You receive ONE JSON object with an "episodes" array.
- Each element has:
  {{
    "episode_id": 123,
    "title_original": "<string>",
    "description_original": "<string>",
    "language": "<hint, e.g. 'jpn', 'eng', 'zho'>"
  }}
- Episodes in the same batch come from one series and may span multiple seasons.
- Use local context to keep naming and phrasing consistent across the whole batch.

OUTPUT FORMAT
- Return ONLY valid JSON object with key "episodes":
  {{
    "episodes": [
      {{
        "episode_id": 123,
        "title": "<string>",
        "description": "<string>"
      }}
    ]
  }}
- Every input episode_id must appear at most once in the output.
"""
        + _placeholder_rules(target_language_name)
    )
_openai_client: Optional[AsyncOpenAI] = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    _openai_client = AsyncOpenAI(api_key=key)
    return _openai_client


_ALLOWED_RE = regex.compile(
    r"[^ \p{Latin}\p{Cyrillic}0-9.,!?:;'\"()\-\—–…]",
    flags=regex.V1,
)


def _sanitize(text: str) -> str:
    cleaned = normalize_text(text or "", limit=4_000)
    return normalize_text(_ALLOWED_RE.sub("", cleaned), limit=4_000)


@dataclass(slots=True)
class EpisodeTranslationCandidate:
    episode_id: int
    series_id: int
    season_key: str
    season_sort_key: Tuple[int, int, str]
    order_in_season: int
    lang_hint: str
    title_src: str
    overview_src: str
    existing_target_title: str
    existing_target_overview: str
    target_title_ok: bool
    target_overview_ok: bool


@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def translate_openai(
    title: str,
    overview: str,
    lang: str,
    *,
    target_language_name: str,
) -> Tuple[str, str]:
    user_in = {
        "title_original": title or "",
        "description_original": overview or "",
        "language": lang or "und",
    }

    response = await _get_openai_client().chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": build_system_prompt_single(target_language_name)},
            {"role": "user", "content": json.dumps(user_in, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )

    content = (response.choices[0].message.content or "").strip()
    data = json.loads(content)

    title_out = _sanitize(data.get("title", data.get("title_ru", "")))
    description_out = _sanitize(data.get("description", data.get("description_ru", "")))

    return title_out, description_out


def _parse_batch_translation_response(content: str) -> Dict[int, Tuple[str, str]]:
    payload = json.loads(content)
    if isinstance(payload, dict):
        rows = payload.get("episodes", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("Batch translation response must be a JSON object or array")

    if not isinstance(rows, list):
        raise ValueError("Batch translation response has invalid episodes container")

    out: Dict[int, Tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        episode_id = row.get("episode_id")
        if not isinstance(episode_id, int):
            continue
        out[episode_id] = (
            _sanitize(row.get("title", row.get("title_ru", ""))),
            _sanitize(row.get("description", row.get("description_ru", ""))),
        )
    return out


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def translate_openai_batch(
    items: List[EpisodeTranslationCandidate],
    *,
    target_language_name: str,
) -> Dict[int, Tuple[str, str]]:
    user_in = {
        "episodes": [
            {
                "episode_id": item.episode_id,
                "title_original": item.title_src or "",
                "description_original": item.overview_src or "",
                "language": item.lang_hint or "und",
            }
            for item in items
        ]
    }

    response = await _get_openai_client().chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": build_system_prompt_batch(target_language_name)},
            {"role": "user", "content": json.dumps(user_in, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )

    content = (response.choices[0].message.content or "").strip()
    return _parse_batch_translation_response(content)


# ---------------------------------------------------------------------------
# TVDB client
# ---------------------------------------------------------------------------


class APIUnauthorized(Exception):
    """401/403 after token refresh."""


def _norm_lang(code: str) -> str:
    c = (code or "").strip().lower()
    if not c:
        return ""
    mapping = {
        "en": "eng",
        "eng": "eng",
        "ja": "jpn",
        "jp": "jpn",
        "jpn": "jpn",
        "zh": "zho",
        "cn": "zho",
        "zho": "zho",
        "chi": "zho",
        "zhtw": "zhtw",
        "zht": "zht",
        "zhs": "zhs",
    }
    return mapping.get(c, c)


def _extract_episodes(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict):
        eps = data.get("episodes")
        if isinstance(eps, list):
            return [e for e in eps if isinstance(e, dict)]
        eps2 = data.get("data")
        if isinstance(eps2, list):
            return [e for e in eps2 if isinstance(e, dict)]
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    eps3 = payload.get("episodes")
    if isinstance(eps3, list):
        return [e for e in eps3 if isinstance(e, dict)]
    return []


def _extract_season_episodes(season_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    eps = season_data.get("episodes")
    if isinstance(eps, list):
        return [e for e in eps if isinstance(e, dict)]
    return []


def _next_page_from_links(links: Dict[str, Any]) -> Optional[int]:
    """
    TVDB often stores a full URL in links.next; extract the page parameter.
    """
    nxt = links.get("next")
    if not nxt or not isinstance(nxt, str):
        return None
    try:
        q = parse_qs(urlparse(nxt).query)
        pv = q.get("page", [])
        if pv:
            return int(pv[0])
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_search_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
        rows = data.get("results")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _extract_search_result_series_id(item: Dict[str, Any]) -> Optional[int]:
    for key in ("tvdb_id", "id", "objectID", "objectId"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    translations = item.get("translations")
    if isinstance(translations, dict):
        for inner in translations.values():
            if isinstance(inner, dict):
                nested = _extract_search_result_series_id(inner)
                if nested is not None:
                    return nested
    return None


def _extract_genre_names(series_data: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    genres = series_data.get("genres")
    if isinstance(genres, list):
        for genre in genres:
            if isinstance(genre, str):
                names.append(genre)
            elif isinstance(genre, dict):
                name = genre.get("name")
                if isinstance(name, str):
                    names.append(name)
    elif isinstance(genres, dict):
        name = genres.get("name")
        if isinstance(name, str):
            names.append(name)
    return [normalize_text(name, limit=80) for name in names if normalize_text(name, limit=80)]


def _series_is_anime(series_data: Dict[str, Any]) -> bool:
    return any(name.lower() == ANIME_GENRE_NAME.lower() for name in _extract_genre_names(series_data))


def _extract_series_year(series_data: Dict[str, Any]) -> Optional[int]:
    first_air = series_data.get("firstAired") or series_data.get("first_air_time") or series_data.get("year")
    if first_air is None:
        return None
    if isinstance(first_air, int):
        return first_air
    try:
        return int(str(first_air).split("-", 1)[0])
    except ValueError:
        return None


def _limit_series_ids_by_latest_added(
    ids: List[int],
    *,
    latest_added_limit: Optional[int],
    series_rows_by_id: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[int]:
    unique_ids = list(dict.fromkeys(ids))
    ordered_ids = sorted(unique_ids, reverse=True)
    if latest_added_limit is None:
        return ordered_ids

    selected_ids = ordered_ids[:latest_added_limit]
    preview: List[Dict[str, Any]] = []
    rows = series_rows_by_id or {}
    for sid in selected_ids[:10]:
        row = rows.get(sid) or {}
        preview.append(
            {
                "series_id": sid,
                "year": _extract_series_year(row),
                "lastUpdated": row.get("lastUpdated") if isinstance(row, dict) else None,
                "name": normalize_text(row.get("name"), limit=120) if isinstance(row, dict) else "",
            }
        )

    logger.info(
        "Anime discovery: applying latest-added limit={} by TVDB series id desc (API v4 does not expose created timestamp in discovery payloads). Selected preview={}",
        latest_added_limit,
        preview,
    )
    return selected_ids


class TVDBClient:  # pylint: disable=too-few-public-methods
    def __init__(self, api_key: str, pin: Optional[str] = None):
        self._api_key, self._pin = api_key, pin
        self._token: Optional[str] = None
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=50)
        self._client = httpx.AsyncClient(
            base_url=TVDB_BASE_URL,
            timeout=30,
            limits=limits,
            follow_redirects=True,
        )

    async def _login(self) -> str:
        payload: Dict[str, str] = {"apikey": self._api_key}
        if self._pin:
            payload["pin"] = self._pin

        logger.info("TVDB: requesting API token")
        r = await self._client.post("/login", json=payload)
        r.raise_for_status()
        logger.info("TVDB: API token acquired")
        return r.json()["data"]["token"]

    async def _headers(self) -> Dict[str, str]:
        if not self._token:
            self._token = await self._login()
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        max_tries: int = 6,
        quiet_client_error_statuses: Optional[Set[int]] = None,
    ) -> Any:
        last_exc: Optional[Exception] = None
        quiet_statuses = set(quiet_client_error_statuses or ())

        for attempt in range(max_tries):
            try:
                logger.debug("TVDB: {} {} attempt {}/{}", method, url, attempt + 1, max_tries)
                r = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=await self._headers(),
                )

                if r.status_code in {401, 403}:
                    self._token = await self._login()
                    r = await self._client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=await self._headers(),
                    )

                if r.status_code in {401, 403}:
                    raise APIUnauthorized(f"{method} {url} -> {r.status_code}")

                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    wait_s = int(retry_after) if (retry_after and retry_after.isdigit()) else (2**attempt)
                    wait_s = max(1, min(wait_s, 60))
                    logger.warning("TVDB: 429 on {} {}. Sleeping {}s", method, url, wait_s)
                    await asyncio.sleep(wait_s)
                    continue

                if 500 <= r.status_code < 600:
                    wait_s = max(1, min(2**attempt, 30))
                    logger.warning("TVDB: {} on {} {}. Sleeping {}s", r.status_code, method, url, wait_s)
                    await asyncio.sleep(wait_s)
                    continue

                # Do not retry stable 400-499 errors except auth/rate-limit cases.
                if 400 <= r.status_code < 500 and r.status_code not in {401, 403, 429}:
                    if r.status_code not in quiet_statuses:
                        logger.warning(
                            "TVDB: client error {} on {} {} params={} body={}",
                            r.status_code,
                            method,
                            url,
                            params,
                            (r.text or "")[:500],
                        )
                    r.raise_for_status()

                r.raise_for_status()
                return r.json() if r.content else {}

            except HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status is not None and 400 <= status < 500 and status not in {401, 403, 429}:
                    if status not in quiet_statuses:
                        logger.warning(
                            "TVDB: non-retriable client error {} on {} {} params={} body={}",
                            status,
                            method,
                            url,
                            params,
                            ((exc.response.text if exc.response is not None else "") or "")[:500],
                        )
                    raise
                last_exc = exc
                wait_s = max(1, min(2**attempt, 30))
                logger.warning(
                    "TVDB: request error on {} {} (attempt {}/{}): {!r}. Sleeping {}s",
                    method,
                    url,
                    attempt + 1,
                    max_tries,
                    exc,
                    wait_s,
                )
                await asyncio.sleep(wait_s)

            except (httpx.TimeoutException, httpx.NetworkError, APIUnauthorized) as exc:
                last_exc = exc
                wait_s = max(1, min(2**attempt, 30))
                logger.warning(
                    "TVDB: request error on {} {} (attempt {}/{}): {!r}. Sleeping {}s",
                    method,
                    url,
                    attempt + 1,
                    max_tries,
                    exc,
                    wait_s,
                )
                await asyncio.sleep(wait_s)

        if last_exc:
            raise last_exc
        raise RuntimeError(f"TVDB: request failed: {method} {url}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def genres(self) -> List[Dict[str, Any]]:
        return (await self._request("GET", "/genres"))["data"]

    async def series_base(self, series_id: int) -> Dict[str, Any]:
        data = await self._request("GET", f"/series/{series_id}")
        return data.get("data") or {}

    async def series_extended(self, series_id: int) -> Dict[str, Any]:
        data = await self._request("GET", f"/series/{series_id}/extended")
        return data.get("data") or {}

    async def season_extended(self, season_id: int) -> Dict[str, Any]:
        data = await self._request("GET", f"/seasons/{season_id}/extended")
        return data.get("data") or {}

    async def series_filter_by_genre(self, genre_id: int, page: Optional[int] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"genre": genre_id}
        if page is not None:
            params["page"] = page
        return await self._request("GET", "/series/filter", params=params)

    async def search_series(
        self,
        *,
        query: Optional[str] = None,
        company: Optional[str] = None,
        network: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        base_params: Dict[str, Any] = {
            "type": "series",
            "offset": offset,
            "limit": limit,
        }
        if company:
            base_params["company"] = company
        if network:
            base_params["network"] = network

        variants: List[Dict[str, Any]] = []
        if query:
            with_query = dict(base_params)
            with_query["query"] = query
            variants.append(with_query)

            with_q = dict(base_params)
            with_q["q"] = query
            variants.append(with_q)

            query_only = {"query": query, "type": "series", "offset": offset, "limit": limit}
            variants.append(query_only)

        variants.append(base_params)

        last_exc: Optional[Exception] = None
        for params in variants:
            try:
                logger.info("TVDB search_series: trying params={}", params)
                return await self._request("GET", "/search", params=params)
            except HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                last_exc = exc
                logger.warning("TVDB search_series: params rejected with 400: {}", params)

        if last_exc:
            raise last_exc
        raise RuntimeError("TVDB search_series: no valid search params variant")

    async def series_eps(self, series_id: int, season_type: str = "default", page: Optional[int] = None) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/series/{series_id}/episodes/{season_type}",
            params={"page": page} if page is not None else None,
        )

    async def series_eps_with_language(
        self,
        series_id: int,
        season_type: str,
        lang: str,
        page: Optional[int] = None,
    ) -> Dict[str, Any]:
        lang = _norm_lang(lang)
        try:
            return await self._request(
                "GET",
                f"/series/{series_id}/episodes/{season_type}/{lang}",
                params={"page": page} if page is not None else None,
            )
        except HTTPStatusError as exc:
            if exc.response.status_code in {404, 400}:
                return await self._request(
                    "GET",
                    f"/series/{series_id}/episodes/{lang}",
                    params={"page": page} if page is not None else None,
                )
            raise

    async def episode_base(self, eid: int) -> Dict[str, Any]:
        data = await self._request("GET", f"/episodes/{eid}")
        return data.get("data") or {}

    async def series_translation(self, series_id: int, lang: str) -> Dict[str, Any]:
        lang = _norm_lang(lang)
        try:
            data = await self._request(
                "GET",
                f"/series/{series_id}/translations/{lang}",
                quiet_client_error_statuses={404},
            )
            return data.get("data") or {}
        except HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {}
            raise

    async def episode_translation(self, eid: int, lang: str) -> Dict[str, Any]:
        lang = _norm_lang(lang)
        try:
            data = await self._request(
                "GET",
                f"/episodes/{eid}/translations/{lang}",
                quiet_client_error_statuses={404},
            )
            return data.get("data") or {}
        except HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {}
            raise


# ---------------------------------------------------------------------------
# Episode collection
# ---------------------------------------------------------------------------


def _extract_seasons(series_ext: Dict[str, Any]) -> List[Dict[str, Any]]:
    seasons = series_ext.get("seasons")
    if isinstance(seasons, list):
        return [s for s in seasons if isinstance(s, dict)]
    return []


async def collect_episodes_of_season(client: TVDBClient, season_id: int) -> List[Dict[str, Any]]:
    """
    Use /seasons/{id}/extended and read season["episodes"].
    This is more reliable than relying on paginated series episode lists alone.
    """
    season = await client.season_extended(season_id)
    eps = _extract_season_episodes(season)
    logger.info("Season {} -> {} episodes (extended)", season_id, len(eps))
    return eps


async def collect_episodes_of_series_robust(client: TVDBClient, series_id: int) -> List[Dict[str, Any]]:
    """
    Robust collection strategy:
    1) read seasons from /series/{id}/extended, then each /seasons/{seasonId}/extended.
    2) fall back to /series/{id}/episodes/default with links.next pagination.

    In fallback mode, merge existing rows so richer translation flags are not lost.
    """
    by_id: Dict[int, Dict[str, Any]] = {}

    # 1) seasons-based
    try:
        series_ext = await client.series_extended(series_id)
        seasons = _extract_seasons(series_ext)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Series {}: failed to get extended seasons: {!r}", series_id, exc)
        seasons = []

    for s in seasons:
        sid = s.get("id")
        if not isinstance(sid, int):
            continue
        try:
            eps = await collect_episodes_of_season(client, sid)
        except HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            raise
        for e in eps:
            eid = e.get("id")
            if isinstance(eid, int) and eid not in by_id:
                by_id[eid] = e

    # 2) fallback default order
    page: Optional[int] = None
    while True:
        batch = await client.series_eps(series_id, season_type="default", page=page)

        for e in _extract_episodes(batch):
            eid = e.get("id")
            if not isinstance(eid, int):
                continue

            if eid not in by_id:
                by_id[eid] = e
                continue

            # merge: prefer default listing fields (it usually has richer translations info)
            prev = by_id[eid]

            # merge translations lists safely
            for k in ("nameTranslations", "overviewTranslations"):
                a = prev.get(k) or []
                b = e.get(k) or []
                if isinstance(a, list) and isinstance(b, list):
                    merged: List[Any] = []
                    for x in a + b:
                        if x not in merged:
                            merged.append(x)
                    e[k] = merged

            # overwrite old record with enriched one
            prev.update(e)
            by_id[eid] = prev

        next_page = _next_page_from_links(batch.get("links", {}) or {})
        if next_page is None:
            break
        page = next_page

    episodes = list(by_id.values())
    logger.info("Series {}: robust episodes -> {}", series_id, len(episodes))
    return episodes


# ---------------------------------------------------------------------------
# Source-language prefetch and selection
# ---------------------------------------------------------------------------


async def _prefetch_one_lang(
    client: TVDBClient,
    series_id: int,
    season_type: str,
    lang: str,
) -> Tuple[str, Dict[int, Dict[str, str]]]:
    lang = _norm_lang(lang)
    index: Dict[int, Dict[str, str]] = {}
    if not lang:
        return lang, index

    page: Optional[int] = None
    total = 0
    while True:
        try:
            batch = await client.series_eps_with_language(series_id, season_type, lang, page)
        except HTTPStatusError as exc:
            logger.warning(
                "Series {}: cannot prefetch lang={} (status={})",
                series_id,
                lang,
                exc.response.status_code,
            )
            break

        for e in _extract_episodes(batch):
            eid = e.get("id")
            if not isinstance(eid, int):
                continue

            name = (e.get("name") or "").strip()
            ovw = (e.get("overview") or "").strip()

            if _is_placeholder(name):
                name = ""
            if _is_placeholder(ovw):
                ovw = ""

            if not name and not ovw:
                continue

            index[eid] = {"name": name, "overview": ovw}
            total += 1

        next_page = _next_page_from_links(batch.get("links", {}) or {})
        if next_page is None:
            break
        page = next_page

    logger.info("Series {}: prefetched {} episode records for lang={}", series_id, total, lang)
    return lang, index


def _unique_langs(items: List[str]) -> List[str]:
    out: List[str] = []
    for x in items:
        n = _norm_lang(x)
        if n and n not in out:
            out.append(n)
    return out


def decide_source_langs(series_original_language: str, override: Optional[str] = None) -> List[str]:
    orig = _norm_lang(series_original_language)
    override_list: List[str] = []
    if override:
        override_list = _unique_langs([p.strip() for p in override.split(",") if p.strip()])

    if override_list:
        chain = [orig] if orig else []
        chain.extend([lang for lang in override_list if lang != orig])
        if "eng" not in chain:
            chain.append("eng")
        return _unique_langs(chain)

    if orig and orig != "eng":
        return [orig, "eng"]
    return ["eng"]


async def prefetch_series_sources(
    client: TVDBClient,
    series_id: int,
    source_langs: List[str],
    season_type: str = "default",
) -> Dict[int, Dict[str, Dict[str, str]]]:
    tasks = [
        _prefetch_one_lang(client, series_id, season_type, lng)
        for lng in source_langs
        if _norm_lang(lng)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: Dict[int, Dict[str, Dict[str, str]]] = {}
    for res in results:
        if isinstance(res, Exception):
            logger.warning("Prefetch error: {!r}", res)
            continue
        lang, idx = res
        if not lang:
            continue
        for eid, rec in idx.items():
            out.setdefault(eid, {})[lang] = rec
    return out


async def pick_episode_source_text(
    ep: Dict[str, Any],
    *,
    series_original_lang: str,
    source_index: Dict[int, Dict[str, Dict[str, str]]],
    source_langs: List[str],
) -> Tuple[str, str, str]:
    """
    Return (title_src, overview_src, lang_hint).

    Selection strategy:
    - prefer the series originalLanguage;
    - fill gaps from English;
    - fall back to base episode fields.
    """
    eid = ep.get("id")
    if not isinstance(eid, int):
        return "", "", "und"

    orig = _norm_lang(series_original_lang)
    langs = _unique_langs(source_langs)

    base_title = (ep.get("name") or "").strip()
    base_ovw = (ep.get("overview") or "").strip()
    if _is_placeholder(base_title):
        base_title = ""
    if _is_placeholder(base_ovw):
        base_ovw = ""

    src_by_lang = source_index.get(eid, {})

    def get_from_index(lng: str) -> Tuple[str, str]:
        rec = src_by_lang.get(_norm_lang(lng)) or {}
        t = (rec.get("name") or "").strip()
        o = (rec.get("overview") or "").strip()
        if _is_placeholder(t):
            t = ""
        if _is_placeholder(o):
            o = ""
        return t, o

    title = ""
    ovw = ""
    hint = "und"
    for lng in langs:
        t, o = get_from_index(lng)
        if not title and t:
            title = t
            hint = _norm_lang(lng)
        if not ovw and o:
            ovw = o
            if hint == "und":
                hint = _norm_lang(lng)
        if title and ovw:
            break

    if not title:
        title = base_title
    if not ovw:
        ovw = base_ovw
    if hint == "und":
        hint = orig or "eng"

    return title, ovw, hint


# ---------------------------------------------------------------------------
# Episode metadata reconstruction
# ---------------------------------------------------------------------------


def _episode_season_key(ep: Dict[str, Any]) -> str:
    for key in ("seasonNumber", "airedSeason", "seasonId", "season_id"):
        value = ep.get(key)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "series-default"


def _episode_season_sort_key(ep: Dict[str, Any]) -> Tuple[int, int, str]:
    for key in ("seasonNumber", "airedSeason", "seasonId", "season_id"):
        value = ep.get(key)
        if isinstance(value, int):
            return (0, value, str(value))
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                continue
            if cleaned.isdigit():
                return (0, int(cleaned), cleaned)
            match = re.search(r"\d+", cleaned)
            if match:
                return (0, int(match.group()), cleaned.lower())
            return (1, 0, cleaned.lower())
    return (2, 0, "series-default")


def _episode_order_key(ep: Dict[str, Any]) -> int:
    for key in ("number", "airedEpisodeNumber", "episodeNumber", "absoluteNumber", "id"):
        value = ep.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


async def prepare_episode_translation(
    client: TVDBClient,
    ep: Dict[str, Any],
    *,
    series_id: int,
    series_original_lang: str,
    source_index: Dict[int, Dict[str, Dict[str, str]]],
    source_langs: List[str],
    target_lang: str,
    diagnostics: Optional[Dict[str, int]] = None,
) -> Optional[EpisodeTranslationCandidate]:
    _bump_preparation_diagnostic(diagnostics, "total")
    eid = ep.get("id")
    if not isinstance(eid, int):
        _bump_preparation_diagnostic(diagnostics, "invalid_episode_id")
        return None

    target_lang = _norm_lang(target_lang) or DEFAULT_TARGET_LANGUAGE

    # If translation lists are missing/not lists, still check the target-language endpoint.
    name_trs = ep.get("nameTranslations")
    ovw_trs = ep.get("overviewTranslations")

    trs_list_missing = not isinstance(name_trs, list) or not isinstance(ovw_trs, list)

    has_target_flag = False
    if isinstance(name_trs, list) and target_lang in name_trs:
        has_target_flag = True
    if isinstance(ovw_trs, list) and target_lang in ovw_trs:
        has_target_flag = True

    existing_target_title = ""
    existing_target_overview = ""

    if has_target_flag or trs_list_missing:
        target_translation = await client.episode_translation(eid, target_lang)
        existing_target_title = (target_translation.get("name") or "").strip()
        existing_target_overview = (target_translation.get("overview") or "").strip()

    target_title_ok = bool(existing_target_title and not _is_placeholder(existing_target_title))
    target_overview_ok = bool(existing_target_overview and not _is_placeholder(existing_target_overview))

    if target_title_ok and target_overview_ok:
        _bump_preparation_diagnostic(diagnostics, "already_has_full_target")
        logger.debug("Ep {}: target language {} already complete -> skip", eid, target_lang)
        return None

    title_src, ovw_src, lang_hint = await pick_episode_source_text(
        ep,
        series_original_lang=series_original_lang,
        source_index=source_index,
        source_langs=source_langs,
    )

    title_src = (title_src or "").strip()
    ovw_src = (ovw_src or "").strip()

    if _is_placeholder(title_src):
        title_src = ""
    if _is_placeholder(ovw_src):
        ovw_src = ""

    if (not ovw_src) and (not title_src or _is_generic_episode_title(title_src)):
        _bump_preparation_diagnostic(diagnostics, "generic_title_and_empty_overview")
        logger.debug("Ep {}: numeric-only title + empty overview -> skip", eid)
        return None

    if not title_src and not ovw_src:
        _bump_preparation_diagnostic(diagnostics, "no_source_text")
        logger.debug("Ep {}: nothing to translate", eid)
        return None

    # Reconstruct only missing target-language fields.
    title_for_llm = "" if target_title_ok else title_src
    ovw_for_llm = "" if target_overview_ok else ovw_src

    if not title_for_llm and not ovw_for_llm:
        _bump_preparation_diagnostic(diagnostics, "missing_source_for_remaining_fields")
        logger.debug("Ep {}: nothing needed from LLM after target-language check", eid)
        return None

    _bump_preparation_diagnostic(diagnostics, "prepared")
    return EpisodeTranslationCandidate(
        episode_id=eid,
        series_id=series_id,
        season_key=_episode_season_key(ep),
        season_sort_key=_episode_season_sort_key(ep),
        order_in_season=_episode_order_key(ep),
        lang_hint=lang_hint,
        title_src=title_for_llm,
        overview_src=ovw_for_llm,
        existing_target_title=existing_target_title,
        existing_target_overview=existing_target_overview,
        target_title_ok=target_title_ok,
        target_overview_ok=target_overview_ok,
    )


def finalize_episode_translation(
    candidate: EpisodeTranslationCandidate,
    *,
    target_title_new: str,
    target_overview_new: str,
) -> Optional[Dict[str, Any]]:
    target_title_new = "" if _is_placeholder(target_title_new) else (target_title_new or "").strip()
    target_overview_new = "" if _is_placeholder(target_overview_new) else (target_overview_new or "").strip()

    final_target_title = candidate.existing_target_title if candidate.target_title_ok else target_title_new
    final_target_overview = (
        candidate.existing_target_overview if candidate.target_overview_ok else target_overview_new
    )

    if _is_placeholder(final_target_title):
        final_target_title = ""
    if _is_placeholder(final_target_overview):
        final_target_overview = ""

    if not final_target_title and not final_target_overview:
        logger.debug("Ep {}: both target fields empty after merge -> skip", candidate.episode_id)
        return None

    return {
        "episode_id": candidate.episode_id,
        "name": final_target_title,
        "overview": final_target_overview,
        "status": "translated",
        "source_lang": candidate.lang_hint,
        "source_name": candidate.title_src,
        "source_overview": candidate.overview_src,
    }


async def handle_episode(
    client: TVDBClient,
    ep: Dict[str, Any],
    *,
    series_id: int,
    series_original_lang: str,
    source_index: Dict[int, Dict[str, Dict[str, str]]],
    source_langs: List[str],
    target_language_name: str,
    target_lang: str,
) -> Optional[EpisodeTranslationCandidate]:
    candidate = await prepare_episode_translation(
        client,
        ep,
        series_id=series_id,
        series_original_lang=series_original_lang,
        source_index=source_index,
        source_langs=source_langs,
        target_lang=target_lang,
    )
    if candidate is None:
        return None

    target_title_new, target_overview_new = await translate_openai(
        candidate.title_src,
        candidate.overview_src,
        candidate.lang_hint,
        target_language_name=target_language_name,
    )
    return finalize_episode_translation(
        candidate,
        target_title_new=target_title_new,
        target_overview_new=target_overview_new,
    )


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


async def dump_translations(path: str, translations: Dict[int, PipelineItem]) -> None:
    save_pipeline_items(path, translations.values())


def load_translations_checkpoint(path: str) -> List[PipelineItem]:
    return load_pipeline_items(path)


def load_skip_ids(path: Optional[str]) -> Set[int]:
    ids: Set[int] = set()
    if not path:
        return ids
    if not os.path.exists(path):
        logger.info("Skip-list file {} not found", path)
        return ids
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            m = re.search(r"\d+", line)
            if m:
                ids.add(int(m.group()))
    logger.info("Skip-list loaded: {} IDs", len(ids))
    return ids


def _match_key(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", (value or "").strip().lower())


def _extract_network_names(series_data: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()

    def add(value: str) -> None:
        cleaned = normalize_text(value, limit=120)
        if not cleaned:
            return
        key = _match_key(cleaned)
        if not key or key in seen:
            return
        seen.add(key)
        names.append(cleaned)

    def visit(node: Any) -> None:
        if isinstance(node, str):
            add(node)
            return

        if isinstance(node, list):
            for item in node:
                visit(item)
            return

        if not isinstance(node, dict):
            return

        for key in ("name", "companyName", "translatedName", "title"):
            value = node.get(key)
            if isinstance(value, str):
                add(value)

        for key in (
            "network",
            "originalNetwork",
            "latestNetwork",
            "primaryCompany",
            "parentCompany",
            "companies",
            "company",
            "networks",
        ):
            value = node.get(key)
            if isinstance(value, (str, dict, list)):
                visit(value)

    for field in ("network", "originalNetwork", "latestNetwork", "companies", "company", "networks"):
        value = series_data.get(field)
        if isinstance(value, (str, dict, list)):
            visit(value)

    return names


def _network_matches(series_data: Dict[str, Any], wanted_networks: List[str]) -> bool:
    if not wanted_networks:
        return True

    actual_keys = [_match_key(name) for name in _extract_network_names(series_data)]
    actual_keys = [key for key in actual_keys if key]
    if not actual_keys:
        return False

    wanted_keys = [_match_key(name) for name in wanted_networks]
    wanted_keys = [key for key in wanted_keys if key]

    return any(wanted in actual for wanted in wanted_keys for actual in actual_keys)


def summarize_unusable_episode_sources(episodes: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {
        "total": 0,
        "generic_or_empty_title": 0,
        "empty_overview": 0,
        "generic_title_and_empty_overview": 0,
    }

    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        summary["total"] += 1
        title = normalize_text(episode.get("name"), limit=240)
        overview = normalize_text(episode.get("overview"), limit=4000)

        title_bad = (not title) or _is_placeholder(title)
        overview_bad = (not overview) or _is_placeholder(overview)

        if title_bad:
            summary["generic_or_empty_title"] += 1
        if overview_bad:
            summary["empty_overview"] += 1
        if title_bad and overview_bad:
            summary["generic_title_and_empty_overview"] += 1

    return summary


def new_preparation_diagnostics() -> Dict[str, int]:
    return {
        "total": 0,
        "prepared": 0,
        "already_has_full_target": 0,
        "generic_title_and_empty_overview": 0,
        "no_source_text": 0,
        "missing_source_for_remaining_fields": 0,
        "invalid_episode_id": 0,
    }


def _bump_preparation_diagnostic(diagnostics: Optional[Dict[str, int]], key: str) -> None:
    if diagnostics is None:
        return
    diagnostics[key] = diagnostics.get(key, 0) + 1


def log_no_translation_candidates(
    scope: str,
    *,
    diagnostics: Dict[str, int],
    unusable: Dict[str, int],
) -> None:
    total = diagnostics.get("total", 0)
    already_has_full_target = diagnostics.get("already_has_full_target", 0)
    if total and already_has_full_target == total:
        logger.info(
            "{}: prepared 0 candidates because all {} episodes already have complete target-language metadata.",
            scope,
            total,
        )
        return

    logger.warning(
        "{}: no reconstruction candidates prepared. Preparation summary: total_episodes={} already_has_full_target={} generic_title_and_empty_overview={} no_source_text={} missing_source_for_remaining_fields={} invalid_episode_id={} base_generic_or_empty_title={} base_empty_overview={} base_generic_title_and_empty_overview={}.",
        scope,
        total,
        already_has_full_target,
        diagnostics.get("generic_title_and_empty_overview", 0),
        diagnostics.get("no_source_text", 0),
        diagnostics.get("missing_source_for_remaining_fields", 0),
        diagnostics.get("invalid_episode_id", 0),
        unusable["generic_or_empty_title"],
        unusable["empty_overview"],
        unusable["generic_title_and_empty_overview"],
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _chunked(items: List[EpisodeTranslationCandidate], size: int) -> List[List[EpisodeTranslationCandidate]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_episode_batches(
    candidates: List[EpisodeTranslationCandidate],
    *,
    batch_size: int = DEFAULT_TRANSLATION_BATCH_SIZE,
) -> List[List[EpisodeTranslationCandidate]]:
    groups: Dict[int, List[EpisodeTranslationCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.series_id, []).append(candidate)

    ordered_batches: List[List[EpisodeTranslationCandidate]] = []
    for series_id in sorted(groups.keys()):
        group = sorted(
            groups[series_id],
            key=lambda item: (item.season_sort_key, item.order_in_season, item.episode_id),
        )
        ordered_batches.append(group)
    return ordered_batches


async def _translate_candidate_batch_with_fallback(
    candidates: List[EpisodeTranslationCandidate],
    *,
    target_language_name: str,
) -> Dict[int, Tuple[str, str]]:
    if not candidates:
        return {}

    if len(candidates) == 1:
        only = candidates[0]
        title_out, description_out = await translate_openai(
            only.title_src,
            only.overview_src,
            only.lang_hint,
            target_language_name=target_language_name,
        )
        return {only.episode_id: (title_out, description_out)}

    try:
        translated = await translate_openai_batch(candidates, target_language_name=target_language_name)
        if not translated:
            raise ValueError("batch translation returned no episode mappings")

        missing_ids = [candidate.episode_id for candidate in candidates if candidate.episode_id not in translated]
        if missing_ids:
            raise ValueError(f"batch translation missed episode ids: {missing_ids}")
        return translated
    except Exception as exc:  # noqa: BLE001
        if len(candidates) > 2:
            midpoint = len(candidates) // 2
            logger.warning(
                "Batch translation failed for series {} episodes {}-item batch: {!r}. Splitting into {} and {} episodes to preserve context.",
                candidates[0].series_id,
                len(candidates),
                exc,
                midpoint,
                len(candidates) - midpoint,
            )
            left = await _translate_candidate_batch_with_fallback(
                candidates[:midpoint],
                target_language_name=target_language_name,
            )
            right = await _translate_candidate_batch_with_fallback(
                candidates[midpoint:],
                target_language_name=target_language_name,
            )
            merged = dict(left)
            merged.update(right)
            return merged

        logger.warning(
            "Batch translation failed for series {} season {} episodes {}: {!r}. Falling back to single-episode translation.",
            candidates[0].series_id,
            candidates[0].season_key,
            [candidate.episode_id for candidate in candidates],
            exc,
        )
        out: Dict[int, Tuple[str, str]] = {}
        for candidate in candidates:
            out[candidate.episode_id] = await translate_openai(
                candidate.title_src,
                candidate.overview_src,
                candidate.lang_hint,
                target_language_name=target_language_name,
            )
        return out


async def process_episode_batch(
    candidates: List[EpisodeTranslationCandidate],
    *,
    translations: Dict[int, PipelineItem],
    lock: asyncio.Lock,
    skip_ids: Set[int],
    sem: asyncio.Semaphore,
    checkpoint_path: str,
    max_new_items: Optional[int],
    target_language_name: str,
) -> None:
    if max_new_items is not None and len(translations) >= max_new_items:
        return

    async with sem:
        prepared = [
            candidate
            for candidate in candidates
            if candidate.episode_id not in skip_ids
        ]
        if max_new_items is not None:
            remaining = max_new_items - len(translations)
            if remaining <= 0:
                return
            prepared = prepared[:remaining]

        if not prepared:
            if candidates:
                skipped_ids = [candidate.episode_id for candidate in candidates if candidate.episode_id in skip_ids]
                preview = skipped_ids[:20]
                suffix = "..." if len(skipped_ids) > len(preview) else ""
                logger.info(
                    "Series {} season {}: all {} prepared candidates skipped by skip-list ids={}{}",
                    candidates[0].series_id,
                    candidates[0].season_key,
                    len(candidates),
                    preview,
                    suffix,
                )
            return

        translated_map = await _translate_candidate_batch_with_fallback(
            prepared,
            target_language_name=target_language_name,
        )

    items_to_save: List[PipelineItem] = []
    saved_ids: List[int] = []
    for candidate in prepared:
        translated = translated_map.get(candidate.episode_id)
        if translated is None:
            continue
        res = finalize_episode_translation(
            candidate,
            target_title_new=translated[0],
            target_overview_new=translated[1],
        )
        if not res:
            continue
        item = PipelineItem.from_dict(res)
        if item is None:
            continue
        if not item.translated_at:
            item.translated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        items_to_save.append(item)
        saved_ids.append(item.episode_id)

    if not items_to_save:
        return

    async with lock:
        for item in items_to_save:
            if max_new_items is not None and len(translations) >= max_new_items and item.episode_id not in translations:
                break
            existing = translations.get(item.episode_id)
            translations[item.episode_id] = merge_pipeline_items(existing, item) if existing else item
            skip_ids.add(item.episode_id)

        if len(translations) % 10 == 0:
            await dump_translations(checkpoint_path, translations)


async def translate_series(
    client: TVDBClient,
    series_id: int,
    *,
    sem: asyncio.Semaphore,
    translations: Dict[int, PipelineItem],
    lock: asyncio.Lock,
    skip_ids: Set[int],
    source_langs_override: Optional[str],
    include_episode_ids: List[int],
    checkpoint_path: str,
    max_new_items: Optional[int],
    target_lang: str,
    target_language_name: str,
) -> None:
    logger.info("Series {}: start processing", series_id)

    if max_new_items is not None and len(translations) >= max_new_items:
        logger.info("Series {}: max_new_items={} already reached, skipping", series_id, max_new_items)
        return

    series = await client.series_base(series_id)
    original_lang = (series.get("originalLanguage") or "").strip()
    source_langs = decide_source_langs(original_lang, override=source_langs_override)
    logger.info("Series {}: source langs order = {}", series_id, source_langs)

    episodes = await collect_episodes_of_series_robust(client, series_id)

    # Explicit episode inclusion.
    if include_episode_ids:
        present = {e.get("id") for e in episodes if isinstance(e.get("id"), int)}
        for eid in include_episode_ids:
            if eid in present:
                continue
            try:
                base = await client.episode_base(eid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Include episode {}: failed to fetch base: {!r}", eid, exc)
                continue
            if isinstance(base, dict) and base.get("id") == eid:
                episodes.append(base)
                logger.info("Included episode {} explicitly", eid)

    source_index = await prefetch_series_sources(client, series_id, source_langs, season_type="default")
    prepared_for_batching: List[EpisodeTranslationCandidate] = []
    preparation_diagnostics = new_preparation_diagnostics()
    for episode in episodes:
        candidate = await prepare_episode_translation(
            client,
            episode,
            series_id=series_id,
            series_original_lang=original_lang,
            source_index=source_index,
            source_langs=source_langs,
            target_lang=target_lang,
            diagnostics=preparation_diagnostics,
        )
        if candidate is not None:
            prepared_for_batching.append(candidate)

    batches = build_episode_batches(prepared_for_batching)
    logger.info("Series {}: prepared {} candidates in {} batches", series_id, len(prepared_for_batching), len(batches))

    if episodes and not prepared_for_batching:
        unusable = summarize_unusable_episode_sources(episodes)
        log_no_translation_candidates(
            f"Series {series_id}",
            diagnostics=preparation_diagnostics,
            unusable=unusable,
        )

    await asyncio.gather(
        *(
            process_episode_batch(
                batch,
                translations=translations,
                lock=lock,
                skip_ids=skip_ids,
                sem=sem,
                checkpoint_path=checkpoint_path,
                max_new_items=max_new_items,
                target_language_name=target_language_name,
            )
            for batch in batches
        ),
        return_exceptions=True,
    )


# ---------------------------------------------------------------------------
# all-anime helper
# ---------------------------------------------------------------------------


async def _search_series_candidates_by_network(
    client: TVDBClient,
    wanted_networks: List[str],
) -> Dict[int, Dict[str, Any]]:
    candidates: Dict[int, Dict[str, Any]] = {}
    search_limit = 100

    for term in wanted_networks:
        for mode in ("network", "company"):
            offset = 0
            while True:
                logger.info(
                    "Network discovery: searching mode={} term={} offset={} limit={}",
                    mode,
                    term,
                    offset,
                    search_limit,
                )
                payload = await client.search_series(
                    query=term,
                    network=term if mode == "network" else None,
                    company=term if mode == "company" else None,
                    offset=offset,
                    limit=search_limit,
                )
                rows = _extract_search_results(payload)
                logger.info(
                    "Network discovery: mode={} term={} offset={} returned {} rows",
                    mode,
                    term,
                    offset,
                    len(rows),
                )

                added = 0
                for row in rows:
                    sid = _extract_search_result_series_id(row)
                    if sid is None:
                        continue
                    if sid not in candidates:
                        candidates[sid] = row
                        added += 1

                logger.info(
                    "Network discovery: mode={} term={} offset={} added {} new candidate ids (total={})",
                    mode,
                    term,
                    offset,
                    added,
                    len(candidates),
                )

                if len(rows) < search_limit:
                    break
                offset += len(rows)

    return candidates


async def fetch_all_anime_series_ids(
    client: TVDBClient,
    year: Optional[int] = None,
    network: Optional[str] = None,
    latest_added_limit: Optional[int] = None,
) -> List[int]:
    wanted_networks = [part.strip() for part in (network or "").split(",") if part.strip()]

    if wanted_networks:
        logger.info(
            "Anime discovery: network-first path year_filter={} network_filter={}",
            year if year is not None else "none",
            wanted_networks,
        )
        candidates = await _search_series_candidates_by_network(client, wanted_networks)
        logger.info("Anime discovery: network search produced {} unique candidate series ids", len(candidates))

        ids: List[int] = []
        accepted_rows_by_id: Dict[int, Dict[str, Any]] = {}
        hydrated = 0
        accepted_from_search = 0
        accepted_after_extended = 0
        skipped_by_search_year = 0
        skipped_by_search_genre = 0

        for index, (sid, row) in enumerate(candidates.items(), 1):
            logger.info("Anime discovery: validating network candidate {}/{} series {}", index, len(candidates), sid)

            row_year = _extract_series_year(row)
            row_is_anime = _series_is_anime(row)
            row_genres = _extract_genre_names(row)
            row_lang = normalize_text(row.get("originalLanguage"), limit=16).lower() if isinstance(row, dict) else ""

            year_ok = year is None or row_year == year
            if row_is_anime and year_ok and row_lang:
                ids.append(sid)
                accepted_rows_by_id[sid] = row
                accepted_from_search += 1
                continue

            if year is not None and row_year is not None and row_year != year:
                skipped_by_search_year += 1
                logger.info(
                    "Anime discovery: series {} skipped by search year mismatch search_year={} expected_year={}",
                    sid,
                    row_year,
                    year,
                )
                continue

            if row_genres and not row_is_anime:
                skipped_by_search_genre += 1
                logger.info(
                    "Anime discovery: series {} skipped by search genres {}",
                    sid,
                    row_genres,
                )
                continue

            logger.info(
                "Anime discovery: series {} needs extended validation search_genres={} year_in_search={} original_lang={}",
                sid,
                row_genres if row_genres else "none",
                row_year if row_year is not None else "none",
                row_lang or "none",
            )
            hydrated += 1
            try:
                series_ext = await client.series_extended(sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Series {}: failed to load extended metadata: {!r}", sid, exc)
                continue

            ext_lang = normalize_text(series_ext.get("originalLanguage"), limit=16).lower()
            ext_year = _extract_series_year(series_ext)
            if not _series_is_anime(series_ext):
                continue
            if year is not None and ext_year != year:
                continue
            if not ext_lang:
                continue

            ids.append(sid)
            accepted_rows_by_id[sid] = series_ext
            accepted_after_extended += 1

        logger.info(
            "Anime discovery totals: network_candidates={} hydrated={} accepted_from_search={} accepted_after_extended={} skipped_by_search_year={} skipped_by_search_genre={} accepted_total={}",
            len(candidates),
            hydrated,
            accepted_from_search,
            accepted_after_extended,
            skipped_by_search_year,
            skipped_by_search_genre,
            len(ids),
        )
        if year is not None:
            logger.info("After filters: {} anime series with firstAired in {} and network {}", len(ids), year, wanted_networks)
        else:
            logger.info("After filters: {} anime series for network {}", len(ids), wanted_networks)
        return _limit_series_ids_by_latest_added(
            ids,
            latest_added_limit=latest_added_limit,
            series_rows_by_id=accepted_rows_by_id,
        )

    logger.info("Anime discovery: loading genre list")
    genres = await client.genres()
    anime_id = next(
        (g["id"] for g in genres if g.get("name", "").lower() == ANIME_GENRE_NAME.lower()),
        None,
    )
    if not anime_id:
        raise RuntimeError("Genre 'Anime' not found in TVDB list")

    logger.info(
        "Anime discovery: genre-filter path genre_id={} year_filter={} original_language=japanese",
        anime_id,
        year if year is not None else "none",
    )
    ids: List[int] = []
    accepted_rows_by_id: Dict[int, Dict[str, Any]] = {}
    page: Optional[int] = None
    page_no = 0
    total_rows = 0
    total_with_orig_lang = 0
    total_with_japanese_orig_lang = 0
    total_year_ok = 0
    while True:
        page_no += 1
        logger.info("Anime discovery: fetching filter page {}", page_no)
        data = await client.series_filter_by_genre(anime_id, page)
        rows = data.get("data", [])
        if not isinstance(rows, list):
            rows = []
        logger.info("Anime discovery: page {} returned {} rows", page_no, len(rows))

        page_year_ok = 0
        page_accepted = 0
        page_japanese_lang = 0

        for s in rows:
            if not isinstance(s, dict):
                continue
            total_rows += 1
            lang = s.get("originalLanguage")
            if not lang:
                continue
            total_with_orig_lang += 1
            if not _is_japanese_original_language(lang):
                continue
            total_with_japanese_orig_lang += 1
            page_japanese_lang += 1

            first_air = s.get("firstAired")
            if not first_air:
                if year is not None:
                    continue
                sid = s.get("id")
                if not isinstance(sid, int):
                    continue
                total_year_ok += 1
                page_year_ok += 1

                ids.append(sid)
                accepted_rows_by_id[sid] = s
                page_accepted += 1
                continue

            try:
                year_val = int(str(first_air).split("-", 1)[0])
            except ValueError:
                continue

            if (year is not None) and (year_val != year):
                continue
            total_year_ok += 1
            page_year_ok += 1

            sid = s.get("id")
            if not isinstance(sid, int):
                continue

            ids.append(sid)
            accepted_rows_by_id[sid] = s
            page_accepted += 1

        logger.info(
            "Anime discovery: page {} summary accepted={} japanese_lang={} year_ok={} total_accepted={}",
            page_no,
            page_accepted,
            page_japanese_lang,
            page_year_ok,
            len(ids),
        )

        next_page = _next_page_from_links(data.get("links", {}) or {})
        if next_page is None:
            break
        page = next_page

    logger.info(
        "Anime discovery totals: raw_rows={}, rows_with_original_language={}, rows_with_japanese_original_language={}, year_ok={}, accepted={}",
        total_rows,
        total_with_orig_lang,
        total_with_japanese_orig_lang,
        total_year_ok,
        len(ids),
    )
    if year is not None:
        logger.info("After filters: {} japanese anime series with firstAired in {}", len(ids), year)
    else:
        logger.info("After filters: {} japanese anime series (all years)", len(ids))
    return _limit_series_ids_by_latest_added(
        ids,
        latest_added_limit=latest_added_limit,
        series_rows_by_id=accepted_rows_by_id,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(opts: argparse.Namespace) -> None:
    ensure_env_loaded(("TVDB_API_KEY", "OPENAI_API_KEY"))
    target_lang = _norm_lang(opts.target_lang or DEFAULT_TARGET_LANGUAGE) or DEFAULT_TARGET_LANGUAGE
    target_language_name = _target_language_name(target_lang, opts.target_language_name or os.getenv("TARGET_LANGUAGE_NAME"))
    logger.info("Using OpenAI model: {}", DEFAULT_OPENAI_MODEL)
    logger.info("Target language: {} ({})", target_lang, target_language_name)
    logger.info(
        "Run options: season_id={}, series_id={}, all_anime={}, year={}, network={}, latest_added_limit={}, push={}, push_only={}, concurrent={}, max_new_items={}",
        opts.season_id,
        opts.series_id,
        opts.all_anime,
        opts.year,
        opts.network or "none",
        opts.latest_added_limit if opts.latest_added_limit is not None else "none",
        opts.push,
        opts.push_only,
        opts.concurrent,
        opts.max_new_items if opts.max_new_items is not None else "none",
    )

    existing = load_translations_checkpoint(opts.checkpoint_file)
    translations: Dict[int, PipelineItem] = {item.episode_id: item for item in existing}

    skip_ids: Set[int] = set()
    for item in existing:
        skip_ids.add(item.episode_id)

    if opts.ignore_processed_log:
        logger.info("Processed episode log disabled for this run: {}", DEFAULT_PROCESSED_LOG_FILE)
    else:
        processed_log_skip_ids = load_skip_ids(DEFAULT_PROCESSED_LOG_FILE)
        if processed_log_skip_ids:
            logger.info("Loaded {} processed episode IDs from {}", len(processed_log_skip_ids), DEFAULT_PROCESSED_LOG_FILE)
            skip_ids |= processed_log_skip_ids

    if opts.skip_file:
        skip_ids |= load_skip_ids(opts.skip_file)

    logger.info("Initial skip-list size: {} episode IDs", len(skip_ids))

    if opts.push_only:
        logger.info("Push-only mode: starting checkpoint upload")
        await push_checkpoint(
            opts.checkpoint_file,
            state_path=opts.push_state_file,
            headless=opts.push_headless,
            slow_mo_ms=opts.push_slow_mo_ms,
            sleep_between=opts.push_sleep_between,
            max_retries=opts.push_max_retries,
            max_items=opts.push_max_items,
            target_lang=target_lang,
        )
        return

    tvdb_key = os.getenv("TVDB_API_KEY")
    if not tvdb_key:
        logger.error("TVDB_API_KEY is not set")
        return

    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY is not set")
        return

    logger.info("TVDB/OpenAI credentials detected, building API client")
    client = TVDBClient(tvdb_key, os.getenv("TVDB_PIN"))

    try:
        logger.info("TVDB connectivity check: requesting genres")
        await client.genres()
        logger.info("TVDB connectivity check: OK")
    except Exception as exc:  # noqa: BLE001
        logger.error("TVDB: authorization / connectivity check failed: {!r}", exc)
        await client.aclose()
        return

    lock = asyncio.Lock()
    sem = asyncio.Semaphore(opts.concurrent)

    if opts.season_id:
        logger.info("Season mode: collecting season {}", opts.season_id)
        eps = await collect_episodes_of_season(client, opts.season_id)
        logger.info("Season mode: {} episodes returned", len(eps))

        source_index: Dict[int, Dict[str, Dict[str, str]]] = {}
        source_langs = _unique_langs(["jpn", "eng"])
        prepared_for_batching: List[EpisodeTranslationCandidate] = []
        preparation_diagnostics = new_preparation_diagnostics()
        for episode in eps:
            candidate = await prepare_episode_translation(
                client,
                episode,
                series_id=0,
                series_original_lang="",
                source_index=source_index,
                source_langs=source_langs,
                target_lang=target_lang,
                diagnostics=preparation_diagnostics,
            )
            if candidate is not None:
                prepared_for_batching.append(candidate)

        batches = build_episode_batches(prepared_for_batching)
        logger.info("Season mode: prepared {} candidates in {} batches", len(prepared_for_batching), len(batches))

        if eps and not prepared_for_batching:
            unusable = summarize_unusable_episode_sources(eps)
            log_no_translation_candidates(
                f"Season {opts.season_id}",
                diagnostics=preparation_diagnostics,
                unusable=unusable,
            )

        await asyncio.gather(
            *(
                process_episode_batch(
                    batch,
                    translations=translations,
                    lock=lock,
                    skip_ids=skip_ids,
                    sem=sem,
                    checkpoint_path=opts.checkpoint_file,
                    max_new_items=opts.max_new_items,
                    target_language_name=target_language_name,
                )
                for batch in batches
            ),
            return_exceptions=True,
        )

        await dump_translations(opts.checkpoint_file, translations)
        await client.aclose()
        if opts.push:
            await push_checkpoint(
                opts.checkpoint_file,
                state_path=opts.push_state_file,
                headless=opts.push_headless,
                slow_mo_ms=opts.push_slow_mo_ms,
                sleep_between=opts.push_sleep_between,
                max_retries=opts.push_max_retries,
                max_items=opts.push_max_items,
                target_lang=target_lang,
            )
        return

    if opts.all_anime:
        logger.info("All-anime mode: starting discovery")
        series_ids = await fetch_all_anime_series_ids(
            client,
            year=opts.year,
            network=opts.network,
            latest_added_limit=opts.latest_added_limit,
        )
    elif opts.series_id:
        logger.info("Series mode: single series {}", opts.series_id)
        series_ids = [opts.series_id]
    else:
        series_ids = []

    logger.info("Total series to process: {}", len(series_ids))

    await asyncio.gather(
        *(
            translate_series(
                client,
                sid,
                sem=sem,
                translations=translations,
                lock=lock,
                skip_ids=skip_ids,
                source_langs_override=opts.source_langs,
                include_episode_ids=opts.include_episode_id,
                checkpoint_path=opts.checkpoint_file,
                max_new_items=opts.max_new_items,
                target_lang=target_lang,
                target_language_name=target_language_name,
            )
            for sid in series_ids
        ),
        return_exceptions=True,
    )

    await dump_translations(opts.checkpoint_file, translations)
    await client.aclose()

    if opts.push:
        await push_checkpoint(
            opts.checkpoint_file,
            state_path=opts.push_state_file,
            headless=opts.push_headless,
            slow_mo_ms=opts.push_slow_mo_ms,
            sleep_between=opts.push_sleep_between,
            max_retries=opts.push_max_retries,
            max_items=opts.push_max_items,
            target_lang=target_lang,
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk reconstruct localized TVDB episode metadata with an optional review-first push stage."
    )
    mx = p.add_mutually_exclusive_group(required=False)
    mx.add_argument("--season-id", type=int, help="Process one TVDB season id.")
    mx.add_argument("--series-id", type=int, help="Process every available episode for one TVDB series id.")
    mx.add_argument("--all-anime", action="store_true", help="Discover anime series and reconstruct missing episode metadata.")

    p.add_argument("--year", type=int, help="Filter anime discovery by firstAired year. Only valid with --all-anime.")
    p.add_argument("--network", help='Filter by TVDB network/company name, for example "Bilibili" or "Bilibili, Tencent Video".')
    p.add_argument("--latest-added-limit", type=int, help="Limit discovery to the highest TVDB series ids after filtering.")
    p.add_argument("--concurrent", type=int, default=6, help="Maximum concurrent TVDB/provider work.")
    p.add_argument("--skip-file", help="File containing episode IDs to skip. Any number inside a line is accepted.")
    p.add_argument("--ignore-processed-log", action="store_true", help="Do not use the built-in processed log as a skip list.")
    p.add_argument("--source-langs", help='Override source-language order, for example "zho,eng".')
    p.add_argument("--include-episode-id", type=int, action="append", default=[], help="Force an individual episode id into the run. Can be repeated.")
    p.add_argument("--target-lang", default=DEFAULT_TARGET_LANGUAGE, help="TVDB target language code, for example eng, spa, fra.")
    p.add_argument("--target-language-name", help="Human-readable target language name for generation policy.")
    p.add_argument("--max-new-items", type=int, help="Stop after saving this many new generated records to checkpoint.")
    p.add_argument("--checkpoint-file", default=TRANSLATIONS_CHECKPOINT_FILE, help="Path to the JSON checkpoint.")
    p.add_argument("--push", action="store_true", help="After generation, push reviewed checkpoint records through Playwright.")
    p.add_argument("--push-only", action="store_true", help="Skip generation and only push records from checkpoint.")
    p.add_argument("--push-headless", action="store_true", default=DEFAULT_PUSH_HEADLESS, help="Run the push browser without UI.")
    p.add_argument("--push-slow-mo-ms", type=int, default=DEFAULT_PUSH_SLOW_MO_MS, help="Playwright slow-mo in milliseconds.")
    p.add_argument("--push-sleep-between", type=float, default=DEFAULT_PUSH_SLEEP_BETWEEN, help="Pause between push operations.")
    p.add_argument("--push-max-retries", type=int, default=DEFAULT_PUSH_MAX_RETRIES, help="Save retries per episode.")
    p.add_argument("--push-max-items", type=int, help="Limit records in one push run.")
    p.add_argument("--push-state-file", default=DEFAULT_STATE_FILE, help="Path to the Playwright storage_state file.")
    p.add_argument("--log-level", default="INFO", help="Logging level.")
    opts = p.parse_args()

    has_translate_scope = bool(opts.season_id or opts.series_id or opts.all_anime)
    if opts.push_only and has_translate_scope:
        p.error("--push-only cannot be combined with --season-id/--series-id/--all-anime")
    if not opts.push_only and not has_translate_scope:
        p.error("Use one of --season-id/--series-id/--all-anime, or use standalone --push-only")
    if opts.year is not None and not opts.all_anime:
        p.error("--year can only be used with --all-anime")
    if opts.network and not opts.all_anime:
        p.error("--network can only be used with --all-anime")
    if opts.latest_added_limit is not None and not opts.all_anime:
        p.error("--latest-added-limit can only be used with --all-anime")
    if opts.concurrent < 1:
        p.error("--concurrent must be >= 1")
    if opts.push_slow_mo_ms < 0:
        p.error("--push-slow-mo-ms must be >= 0")
    if opts.push_sleep_between < 0:
        p.error("--push-sleep-between must be >= 0")
    if opts.push_max_retries < 1:
        p.error("--push-max-retries must be >= 1")
    if opts.push_max_items is not None and opts.push_max_items < 1:
        p.error("--push-max-items must be >= 1")
    if opts.max_new_items is not None and opts.max_new_items < 1:
        p.error("--max-new-items must be >= 1")
    if opts.latest_added_limit is not None and opts.latest_added_limit < 1:
        p.error("--latest-added-limit must be >= 1")

    return opts


def main() -> None:
    opts = parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level=str(opts.log_level).upper(),
        format="<level>{level}</level> | {time:YYYY-MM-DD HH:mm:ss} | {message}",
    )

    try:
        asyncio.run(main_async(opts))
    finally:
        if os.path.exists(opts.checkpoint_file):
            logger.info("final checkpoint on exit ({})", opts.checkpoint_file)


if __name__ == "__main__":
    main()
