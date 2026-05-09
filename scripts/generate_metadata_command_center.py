#!/usr/bin/env python3
"""Generate the AniMeta Nexus public showcase.

The showcase is intentionally deterministic and credential-free. It seeds a
synthetic demo corpus when the demo files do not exist, reads that corpus back,
then writes a report JSON, SVG assets, and a static GitHub Pages dashboard.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_NAME = "AniMeta Nexus"
TAGLINE = "Stop browsing empty episode pages. Start owning your metadata."
DESCRIPTION = (
    "A TVDB-powered metadata intelligence layer for anime libraries: Signal "
    "Acquisition, Placeholder Suppression, Continuity-Aware Context Assembly, "
    "Semantic Reconstruction, Review Governance, and Contribution-Ready "
    "Distribution in one controlled recovery system."
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DEMO = DOCS / "demo"
ASSETS = DOCS / "assets"
SOURCE_SIGNAL_CORPUS = DEMO / "source_signal_corpus.json"
RECONSTRUCTION_OUTPUT = DEMO / "reconstruction_core_output.json"
COMMAND_CENTER_REPORT = DEMO / "command_center_report.json"
INDEX_HTML = DOCS / "index.html"
HERO_SVG = ASSETS / "metadata_command_surface.svg"
WORKFLOW_SVG = ASSETS / "recovery_workflow_rail.svg"

PLACEHOLDER_TOKENS = {
    "",
    "tba",
    "tbd",
    "tbc",
    "n/a",
    "unknown",
    "unknown title",
    "untitled",
    "to be announced",
    "to be determined",
    "-",
    "--",
    "---",
}


SERIES_CONFIG = [
    {
        "series_id": 810001,
        "season_id": 910001,
        "series_name": "Star Harbor Logbook",
        "source_language": "jpn",
        "target_language": "eng",
        "source_prefix": "Minato Chronicle",
        "title_theme": [
            "Lanterns at the South Pier",
            "The Quiet Signal",
            "A Map Written in Rain",
            "Harbor Watch",
            "The Missing Tide Bell",
            "Night Shift at Dock Seven",
            "Compass Under Glass",
            "A Name Left in the Ledger",
            "Fog Over the Relay Tower",
            "The Captain's Small Lie",
            "Letters from the Outer Buoy",
            "Signal Fire at Dawn",
        ],
    },
    {
        "series_id": 810002,
        "season_id": 910002,
        "series_name": "Clockwork Orchard",
        "source_language": "zho",
        "target_language": "eng",
        "source_prefix": "Orchard Record",
        "title_theme": [
            "The Spring That Would Not Turn",
            "Copper Leaves",
            "A Key Beneath the Roots",
            "The Orchard Wakes",
            "Inventory of Lost Seeds",
            "The Glass Beekeeper",
            "An Hour Borrowed",
            "The Apprentice's Repair",
            "Smoke from the East Gate",
            "A Promise in Brass",
            "The Bell Inside the Tree",
            "Harvest After Midnight",
        ],
    },
    {
        "series_id": 810003,
        "season_id": 910003,
        "series_name": "Northbound After School",
        "source_language": "jpn",
        "target_language": "eng",
        "source_prefix": "Northbound Note",
        "title_theme": [
            "The Last Bus Leaves Early",
            "Notebook on Platform Two",
            "A Detour Through Snow",
            "The Clubroom Key",
            "Borrowed Gloves",
            "The Transfer Student's Route",
            "Cafe at the Terminal",
            "The Station Without a Clock",
            "Message in the Lost-and-Found",
            "Tracks Under Moonlight",
            "The Festival Train",
            "Next Stop, Spring",
        ],
    },
]


def normalize_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def is_placeholder(value: Any) -> bool:
    text = normalize_text(value)
    lowered = " ".join(text.lower().replace(".", " ").split())
    if lowered in PLACEHOLDER_TOKENS:
        return True
    core = "".join(ch for ch in lowered if ch.isalnum())
    if len(core) < 2:
        return True
    if core.isdigit():
        return True
    return False


def issue_flags_for_episode(index: int) -> list[str]:
    flags: list[str] = []
    if index in {1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12}:
        flags.append("missing_target_title")
    if index in {1, 2, 3, 5, 6, 7, 9, 10, 11, 12}:
        flags.append("missing_target_overview")
    if index in {2, 5, 9}:
        flags.append("placeholder_target_title")
    if index == 4:
        flags.append("numeric_source_title")
    if index == 10:
        flags.append("weak_source_overview")
    if index == 8:
        flags.append("existing_valid_metadata")
    return flags


def make_source_signal_corpus() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for series_index, config in enumerate(SERIES_CONFIG):
        for episode_number, title in enumerate(config["title_theme"], start=1):
            episode_id = int(config["series_id"]) * 100 + episode_number
            flags = issue_flags_for_episode(episode_number)
            source_title = f"{config['source_prefix']} {episode_number}"
            source_overview = (
                f"The episode follows a small decision in {config['series_name']} "
                "and leaves enough source context for a clean localized record."
            )
            if "numeric_source_title" in flags:
                source_title = str(episode_number)
            if "weak_source_overview" in flags:
                source_overview = "TBD"

            existing_title = ""
            existing_overview = ""
            if "placeholder_target_title" in flags:
                existing_title = "TBA"
            if "existing_valid_metadata" in flags:
                existing_title = title
                existing_overview = (
                    f"{config['series_name']} already has a concise, usable episode "
                    "summary, so the pipeline preserves it."
                )
            if series_index == 2 and episode_number == 4:
                existing_overview = "A partial summary exists, but the title is still missing."

            records.append(
                {
                    "series_id": config["series_id"],
                    "series_name": config["series_name"],
                    "season_id": config["season_id"],
                    "season_number": 1,
                    "episode_id": episode_id,
                    "episode_number": episode_number,
                    "source_language": config["source_language"],
                    "target_language": config["target_language"],
                    "source_title": source_title,
                    "source_overview": source_overview,
                    "existing_target_title": existing_title,
                    "existing_target_overview": existing_overview,
                    "issue_flags": flags,
                }
            )
    return records


def make_reconstruction_output(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    title_lookup: dict[tuple[int, int], str] = {}
    for config in SERIES_CONFIG:
        for episode_number, title in enumerate(config["title_theme"], start=1):
            title_lookup[(int(config["series_id"]), episode_number)] = title

    status_overrides = {
        81000100 + 12: "pushed",
        81000200 + 7: "generated",
        81000200 + 11: "locked",
        81000300 + 6: "failed",
        81000300 + 9: "generated",
        81000300 + 12: "locked",
    }

    for row in inputs:
        episode_id = int(row["episode_id"])
        episode_number = int(row["episode_number"])
        title = title_lookup[(int(row["series_id"]), episode_number)]
        status = status_overrides.get(episode_id, "review_ready")
        flags = list(row["issue_flags"])
        warnings: list[str] = []
        quality_flags = [
            "placeholder_suppressed",
            "source_ranked",
            "context_packaged",
            "policy_checked",
        ]

        if "existing_valid_metadata" in flags:
            status = "skipped_existing"
            quality_flags = ["existing_valid_metadata_preserved", "non_destructive"]
            generated_title = normalize_text(row["existing_target_title"])
            generated_overview = normalize_text(row["existing_target_overview"])
        elif status == "failed":
            generated_title = ""
            generated_overview = ""
            warnings = ["demo_generation_failed_for_review"]
        else:
            generated_title = title
            generated_overview = (
                f"A clean episode record for {row['series_name']} that keeps the "
                "source intent, removes placeholder noise, and is ready for human review."
            )
            if "weak_source_overview" in flags:
                warnings.append("overview_source_was_weak")
            if status == "locked":
                warnings.append("contribution_target_locked")

        outputs.append(
            {
                "episode_id": episode_id,
                "generated_title": generated_title,
                "generated_overview": generated_overview,
                "status": status,
                "warnings": warnings,
                "quality_flags": quality_flags,
                "reconstruction_notes": (
                    "Synthetic demo record generated from ranked source fields and "
                    "continuity-aware neighboring episode context."
                ),
            }
        )
    return outputs


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fp:
        return json.load(fp)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def seed_demo_files(*, refresh: bool = False) -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    if refresh or not SOURCE_SIGNAL_CORPUS.exists():
        inputs = make_source_signal_corpus()
        write_json(SOURCE_SIGNAL_CORPUS, inputs)
    else:
        inputs = read_json(SOURCE_SIGNAL_CORPUS)
    if refresh or not RECONSTRUCTION_OUTPUT.exists():
        outputs = make_reconstruction_output(inputs)
        write_json(RECONSTRUCTION_OUTPUT, outputs)


def load_demo() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inputs = read_json(SOURCE_SIGNAL_CORPUS)
    outputs = read_json(RECONSTRUCTION_OUTPUT)
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        raise ValueError("Demo input and output files must contain JSON arrays.")
    input_ids = {row.get("episode_id") for row in inputs if isinstance(row, dict)}
    output_ids = {row.get("episode_id") for row in outputs if isinstance(row, dict)}
    missing = sorted(input_ids - output_ids)
    if missing:
        raise ValueError(f"Missing output rows for episode ids: {missing[:8]}")
    return inputs, outputs


def make_report(inputs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    output_by_id = {int(row["episode_id"]): row for row in outputs}
    status_counts = Counter(normalize_text(row.get("status")) or "unknown" for row in outputs)
    source_langs = Counter(normalize_text(row.get("source_language")) for row in inputs)
    target_langs = Counter(normalize_text(row.get("target_language")) for row in inputs)

    incomplete = []
    placeholder_stats = Counter()
    for row in inputs:
        flags = set(row.get("issue_flags") or [])
        title_missing = is_placeholder(row.get("existing_target_title"))
        overview_missing = is_placeholder(row.get("existing_target_overview"))
        if title_missing or overview_missing:
            incomplete.append(row)
        for flag in flags:
            if "placeholder" in flag or "missing" in flag or "numeric" in flag or "weak" in flag:
                placeholder_stats[flag] += 1

    titles_recovered = 0
    summaries_recovered = 0
    suppressed = 0
    for row in inputs:
        out = output_by_id[int(row["episode_id"])]
        if is_placeholder(row.get("existing_target_title")) and not is_placeholder(out.get("generated_title")):
            titles_recovered += 1
        if is_placeholder(row.get("existing_target_overview")) and not is_placeholder(out.get("generated_overview")):
            summaries_recovered += 1
        if any(flag in row.get("issue_flags", []) for flag in ("placeholder_target_title", "numeric_source_title", "weak_source_overview")):
            suppressed += 1

    reconstructed = sum(
        1
        for row in outputs
        if row.get("status") in {"generated", "review_ready", "pushed", "locked"}
    )
    review_ready = status_counts["review_ready"]
    contributed = status_counts["pushed"]
    recovery_rate = round((reconstructed / max(1, len(incomplete))) * 100, 1)

    examples = []
    for row in inputs:
        out = output_by_id[int(row["episode_id"])]
        if out.get("status") not in {"review_ready", "pushed", "locked"}:
            continue
        examples.append(
            {
                "series_name": row["series_name"],
                "episode": f"S{int(row['season_number']):02d}E{int(row['episode_number']):02d}",
                "before_title": row.get("existing_target_title") or "empty",
                "before_overview": row.get("existing_target_overview") or "empty",
                "after_title": out.get("generated_title"),
                "after_overview": out.get("generated_overview"),
                "status": out.get("status"),
                "issue_flags": row.get("issue_flags", []),
            }
        )
        if len(examples) >= 5:
            break

    recent_records = []
    for row in inputs[:24]:
        out = output_by_id[int(row["episode_id"])]
        recent_records.append(
            {
                "episode": f"S{int(row['season_number']):02d}E{int(row['episode_number']):02d}",
                "series_name": row["series_name"],
                "source_language": row["source_language"],
                "target_language": row["target_language"],
                "issue_detected": ", ".join(row.get("issue_flags", [])[:3]) or "none",
                "generated_title": out.get("generated_title") or "not generated",
                "status": out.get("status"),
                "quality_flags": ", ".join(out.get("quality_flags", [])[:2]),
            }
        )

    quality_gates = [
        {
            "name": "Metadata Signal Acquisition Array",
            "copy": "Summons series, season, episode ordering, translation flags, aliases, and source-language hints into one source field.",
        },
        {
            "name": "Metadata Void Cartography Engine",
            "copy": "Maps blank titles, missing summaries, placeholder records, and weak target fields before users fall into them.",
        },
        {
            "name": "Placeholder Suppression Firewall",
            "copy": "Stops TBA, TBD, numeric-only, empty, and punctuation-only debris before reconstruction.",
        },
        {
            "name": "Source Intelligence Layer",
            "copy": "Ranks original-language, fallback, and base fields instead of trusting the first string.",
        },
        {
            "name": "Series Continuity Engine",
            "copy": "Packages neighboring episode signals so recurring titles, numbering, and naming patterns stay consistent.",
        },
        {
            "name": "Localization Policy Engine",
            "copy": "Applies target-language rules for concise, native, database-friendly phrasing.",
        },
        {
            "name": "Non-Destructive Governance Layer",
            "copy": "Protects valid existing fields and authorizes reconstruction only where recovery is needed.",
        },
        {
            "name": "Review-First Publishing Gate",
            "copy": "Stages generated records for human inspection before export or contribution workflows.",
        },
        {
            "name": "Contribution-Ready Distribution Rail",
            "copy": "Prepares reviewed records for JSON, review pages, sidecar exports, and controlled TVDB-backed contribution workflows.",
        },
    ]

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "project_name": PROJECT_NAME,
        "tagline": TAGLINE,
        "description": DESCRIPTION,
        "demo_summary": {
            "records": len(inputs),
            "series": len({row["series_id"] for row in inputs}),
            "source_languages": dict(sorted(source_langs.items())),
            "target_languages": dict(sorted(target_langs.items())),
        },
        "metrics": {
            "demo_corpus_episodes": len(inputs),
            "metadata_gaps_detected": len(incomplete),
            "titles_recovered": titles_recovered,
            "summaries_recovered": summaries_recovered,
            "placeholder_records_suppressed": suppressed,
            "review_ready_records": review_ready,
            "recovery_rate": recovery_rate,
            "real_workflow_scale_note": "Local runtime has processed large private queues; public demo data is intentionally synthetic.",
        },
        "funnel": [
            {"stage": "Discovered", "count": len(inputs)},
            {"stage": "Incomplete", "count": len(incomplete)},
            {"stage": "Context Packaged", "count": len(incomplete)},
            {"stage": "Reconstructed", "count": reconstructed},
            {"stage": "Review Ready", "count": review_ready},
            {"stage": "Exported / Contributed", "count": contributed},
        ],
        "status_distribution": dict(sorted(status_counts.items())),
        "language_bridge": {
            "source": "Japanese / Chinese / English source signals",
            "target": "Configurable target-language metadata",
            "demo_target": "English demo records",
        },
        "placeholder_stats": dict(sorted(placeholder_stats.items())),
        "before_after_examples": examples,
        "recent_records": recent_records,
        "quality_gates": quality_gates,
        "roadmap": [
            "Cleaner CLI wrapper",
            "Review HTML export",
            "Generic JSON export",
            "NFO-style sidecars",
            "Configurable target-language policies",
            "Guided local mode",
        ],
    }


def esc(value: Any) -> str:
    return html.escape(normalize_text(value), quote=True)


def pct(value: int, maximum: int) -> int:
    if maximum <= 0:
        return 0
    return max(4, min(100, int(round((value / maximum) * 100))))


def write_assets() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    HERO_SVG.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 520" role="img" aria-labelledby="title desc">
  <title id="title">AniMeta Nexus metadata command surface</title>
  <desc id="desc">Abstract episode cards flowing through a metadata reconstruction core.</desc>
  <defs>
    <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#08111f"/>
      <stop offset="0.52" stop-color="#101827"/>
      <stop offset="1" stop-color="#12111f"/>
    </linearGradient>
    <linearGradient id="line" x1="0" x2="1">
      <stop offset="0" stop-color="#38d5ff"/>
      <stop offset="0.5" stop-color="#f4c95d"/>
      <stop offset="1" stop-color="#ff5c8a"/>
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="5" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>
  <rect width="1200" height="520" rx="28" fill="url(#bg)"/>
  <path d="M70 422 C230 292, 360 326, 510 222 S850 94, 1130 174" fill="none" stroke="url(#line)" stroke-width="4" opacity=".8" filter="url(#glow)"/>
  <g fill="none" stroke="#27415f" opacity=".7">
    <path d="M110 80H1090M110 160H1090M110 240H1090M110 320H1090M110 400H1090"/>
    <path d="M170 50V470M320 50V470M470 50V470M620 50V470M770 50V470M920 50V470M1070 50V470"/>
  </g>
  <g transform="translate(90 100)">
    <rect width="230" height="118" rx="16" fill="#101f32" stroke="#2e5c7a"/>
    <text x="24" y="38" fill="#8ce4ff" font-family="Inter,Segoe UI,sans-serif" font-size="17" font-weight="700">Source Signals</text>
    <text x="24" y="70" fill="#d8e8f4" font-family="Inter,Segoe UI,sans-serif" font-size="14">TVDB episode payloads</text>
    <text x="24" y="95" fill="#8aa4b8" font-family="Inter,Segoe UI,sans-serif" font-size="13">aliases, order, languages</text>
  </g>
  <g transform="translate(90 280)">
    <rect width="230" height="118" rx="16" fill="#1d1b2f" stroke="#684f8d"/>
    <text x="24" y="38" fill="#f4c95d" font-family="Inter,Segoe UI,sans-serif" font-size="17" font-weight="700">Void Detection</text>
    <text x="24" y="70" fill="#f0e5c1" font-family="Inter,Segoe UI,sans-serif" font-size="14">missing titles</text>
    <text x="24" y="95" fill="#b7a987" font-family="Inter,Segoe UI,sans-serif" font-size="13">TBA, TBD, empty fields</text>
  </g>
  <g transform="translate(470 138)">
    <rect width="270" height="244" rx="28" fill="#0d1f2a" stroke="#38d5ff" stroke-width="2" filter="url(#glow)"/>
    <text x="135" y="78" text-anchor="middle" fill="#ffffff" font-family="Inter,Segoe UI,sans-serif" font-size="24" font-weight="800">Metadata</text>
    <text x="135" y="111" text-anchor="middle" fill="#ffffff" font-family="Inter,Segoe UI,sans-serif" font-size="24" font-weight="800">Reconstruction</text>
    <text x="135" y="150" text-anchor="middle" fill="#8ce4ff" font-family="Inter,Segoe UI,sans-serif" font-size="14">context + policy + continuity</text>
    <rect x="52" y="184" width="166" height="10" rx="5" fill="#38d5ff"/>
    <rect x="52" y="205" width="125" height="10" rx="5" fill="#f4c95d"/>
  </g>
  <g transform="translate(880 100)">
    <rect width="230" height="118" rx="16" fill="#132319" stroke="#4cb37a"/>
    <text x="24" y="38" fill="#8ff0b2" font-family="Inter,Segoe UI,sans-serif" font-size="17" font-weight="700">Review Ready</text>
    <text x="24" y="70" fill="#dff5e8" font-family="Inter,Segoe UI,sans-serif" font-size="14">localized titles</text>
    <text x="24" y="95" fill="#9db8a6" font-family="Inter,Segoe UI,sans-serif" font-size="13">clean summaries</text>
  </g>
  <g transform="translate(880 280)">
    <rect width="230" height="118" rx="16" fill="#2a1620" stroke="#a84a71"/>
    <text x="24" y="38" fill="#ff8aaa" font-family="Inter,Segoe UI,sans-serif" font-size="17" font-weight="700">Distribution</text>
    <text x="24" y="70" fill="#ffe1e9" font-family="Inter,Segoe UI,sans-serif" font-size="14">JSON, review pages</text>
    <text x="24" y="95" fill="#c7a0ad" font-family="Inter,Segoe UI,sans-serif" font-size="13">future sidecars</text>
  </g>
</svg>
""",
        encoding="utf-8",
    )
    WORKFLOW_SVG.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 180" role="img" aria-labelledby="title desc">
  <title id="title">AniMeta Nexus workflow</title>
  <desc id="desc">Discovery to review-first export workflow.</desc>
  <defs>
    <linearGradient id="rail" x1="0" x2="1">
      <stop offset="0" stop-color="#38d5ff"/>
      <stop offset=".5" stop-color="#f4c95d"/>
      <stop offset="1" stop-color="#ff5c8a"/>
    </linearGradient>
  </defs>
  <rect width="1100" height="180" rx="20" fill="#0c1320"/>
  <path d="M85 92H1015" stroke="url(#rail)" stroke-width="5" stroke-linecap="round"/>
  <g font-family="Inter,Segoe UI,sans-serif" text-anchor="middle">
    <g transform="translate(95 92)"><circle r="32" fill="#12263a" stroke="#38d5ff"/><text y="58" fill="#d9eef8" font-size="14">Discovery</text></g>
    <g transform="translate(275 92)"><circle r="32" fill="#1d1b2f" stroke="#a98cff"/><text y="58" fill="#e8ddff" font-size="14">Gap Detection</text></g>
    <g transform="translate(455 92)"><circle r="32" fill="#2a2116" stroke="#f4c95d"/><text y="58" fill="#fff0be" font-size="14">Source Intelligence</text></g>
    <g transform="translate(635 92)"><circle r="32" fill="#132319" stroke="#58d68d"/><text y="58" fill="#dff5e8" font-size="14">Reconstruction</text></g>
    <g transform="translate(815 92)"><circle r="32" fill="#2a1620" stroke="#ff5c8a"/><text y="58" fill="#ffe1e9" font-size="14">Review</text></g>
    <g transform="translate(995 92)"><circle r="32" fill="#14212b" stroke="#8ce4ff"/><text y="58" fill="#d9eef8" font-size="14">Export</text></g>
  </g>
</svg>
""",
        encoding="utf-8",
    )


def render_metric_cards(metrics: dict[str, Any]) -> str:
    cards = [
        ("Demo corpus", metrics["demo_corpus_episodes"], "synthetic episode records"),
        ("Gaps detected", metrics["metadata_gaps_detected"], "missing or weak target fields"),
        ("Titles recovered", metrics["titles_recovered"], "clean title candidates"),
        ("Summaries recovered", metrics["summaries_recovered"], "review-ready overviews"),
        ("Placeholders suppressed", metrics["placeholder_records_suppressed"], "before generation"),
        ("Review ready", metrics["review_ready_records"], "staged records"),
        ("Recovery rate", f"{metrics['recovery_rate']}%", "demo reconstruction coverage"),
    ]
    return "\n".join(
        f"""
        <article class="metric-card">
          <span>{esc(label)}</span>
          <strong>{esc(value)}</strong>
          <p>{esc(copy)}</p>
        </article>
        """
        for label, value, copy in cards
    )


def render_funnel(funnel: list[dict[str, Any]]) -> str:
    maximum = max(item["count"] for item in funnel)
    return "\n".join(
        f"""
        <div class="funnel-row">
          <div class="funnel-label"><span>{esc(item['stage'])}</span><strong>{esc(item['count'])}</strong></div>
          <div class="bar"><i style="width:{pct(int(item['count']), maximum)}%"></i></div>
        </div>
        """
        for item in funnel
    )


def render_status_distribution(distribution: dict[str, int]) -> str:
    maximum = max(distribution.values()) if distribution else 1
    return "\n".join(
        f"""
        <div class="status-row">
          <span>{esc(status.replace('_', ' '))}</span>
          <div class="bar"><i style="width:{pct(count, maximum)}%"></i></div>
          <strong>{count}</strong>
        </div>
        """
        for status, count in distribution.items()
    )


def render_examples(examples: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"""
        <article class="before-after">
          <div>
            <span class="eyebrow">{esc(example['series_name'])} / {esc(example['episode'])}</span>
            <h3>Before</h3>
            <p><b>Title:</b> {esc(example['before_title'])}</p>
            <p><b>Overview:</b> {esc(example['before_overview'])}</p>
          </div>
          <div>
            <span class="pill">{esc(example['status'])}</span>
            <h3>{esc(example['after_title'])}</h3>
            <p>{esc(example['after_overview'])}</p>
          </div>
        </article>
        """
        for example in examples
    )


def render_quality_gates(gates: list[dict[str, str]]) -> str:
    return "\n".join(
        f"""
        <article class="gate-card">
          <h3>{esc(gate['name'])}</h3>
          <p>{esc(gate['copy'])}</p>
        </article>
        """
        for gate in gates
    )


def render_records(records: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"""
        <tr>
          <td>{esc(row['episode'])}</td>
          <td>{esc(row['series_name'])}</td>
          <td>{esc(row['source_language'])} -> {esc(row['target_language'])}</td>
          <td>{esc(row['issue_detected'])}</td>
          <td>{esc(row['generated_title'])}</td>
          <td><span class="pill">{esc(row['status'])}</span></td>
        </tr>
        """
        for row in records
    )
    return f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Episode</th>
            <th>Series</th>
            <th>Bridge</th>
            <th>Issue detected</th>
            <th>Generated title</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def render_roadmap(items: list[str]) -> str:
    return "\n".join(
        f"""
        <article class="roadmap-card">
          <span>{index:02d}</span>
          <h3>{esc(item)}</h3>
        </article>
        """
        for index, item in enumerate(items, start=1)
    )


def render_html(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(PROJECT_NAME)} - Metadata Command Center</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #070b12;
      --panel: rgba(17, 28, 43, .78);
      --panel-strong: rgba(21, 34, 52, .94);
      --line: rgba(148, 184, 210, .2);
      --text: #eef6fb;
      --muted: #9eb4c6;
      --cyan: #38d5ff;
      --gold: #f4c95d;
      --coral: #ff6b7a;
      --green: #67e09a;
      --violet: #a98cff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        linear-gradient(180deg, rgba(56, 213, 255, .08), transparent 360px),
        linear-gradient(120deg, rgba(255, 107, 122, .08), transparent 520px),
        var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(148,184,210,.055) 1px, transparent 1px),
        linear-gradient(90deg, rgba(148,184,210,.055) 1px, transparent 1px);
      background-size: 56px 56px;
      mask-image: linear-gradient(to bottom, black, transparent 74%);
    }}
    a {{ color: inherit; }}
    .shell {{ width: min(1180px, calc(100% - 36px)); margin: 0 auto; }}
    header {{ padding: 28px 0 18px; position: sticky; top: 0; z-index: 10; backdrop-filter: blur(18px); background: rgba(7,11,18,.72); border-bottom: 1px solid var(--line); }}
    nav {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    .brand {{ font-weight: 850; letter-spacing: 0; }}
    .nav-links {{ display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 14px; }}
    .hero {{ padding: 72px 0 46px; display: grid; grid-template-columns: minmax(0, .9fr) minmax(360px, 1.1fr); gap: 42px; align-items: center; }}
    .eyebrow {{ color: var(--cyan); text-transform: uppercase; letter-spacing: .14em; font-size: 12px; font-weight: 800; }}
    h1 {{ margin: 12px 0 16px; font-size: clamp(46px, 7vw, 88px); line-height: .94; letter-spacing: 0; max-width: 820px; }}
    h2 {{ font-size: clamp(28px, 4vw, 46px); line-height: 1.02; letter-spacing: 0; margin: 0 0 14px; }}
    h3 {{ letter-spacing: 0; margin: 0 0 10px; }}
    p {{ color: var(--muted); margin: 0; }}
    .lead {{ font-size: clamp(18px, 2.2vw, 23px); color: #d9e8f1; max-width: 720px; }}
    .hero-actions {{ margin-top: 26px; display: flex; gap: 12px; flex-wrap: wrap; }}
    .button {{ display: inline-flex; align-items: center; min-height: 42px; border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; background: var(--panel); color: var(--text); text-decoration: none; font-weight: 750; }}
    .button.primary {{ border-color: rgba(56,213,255,.55); background: linear-gradient(135deg, rgba(56,213,255,.26), rgba(255,107,122,.18)); }}
    .hero-visual img {{ display: block; width: 100%; height: auto; border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 24px 80px rgba(0,0,0,.34); }}
    section {{ padding: 54px 0; }}
    .section-head {{ display: flex; justify-content: space-between; align-items: end; gap: 24px; margin-bottom: 22px; }}
    .section-head p {{ max-width: 620px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 12px; }}
    .metric-card, .panel, .gate-card, .before-after, .roadmap-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 18px 55px rgba(0,0,0,.2);
    }}
    .metric-card {{ padding: 18px; min-height: 150px; }}
    .metric-card span {{ color: var(--muted); font-size: 13px; }}
    .metric-card strong {{ display: block; margin: 12px 0 4px; font-size: clamp(30px, 4vw, 48px); line-height: 1; color: var(--text); }}
    .grid-2 {{ display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(320px, .92fr); gap: 18px; }}
    .panel {{ padding: 22px; }}
    .funnel-row, .status-row {{ display: grid; gap: 10px; align-items: center; margin: 14px 0; }}
    .funnel-row {{ grid-template-columns: 190px 1fr; }}
    .status-row {{ grid-template-columns: 135px 1fr 44px; }}
    .funnel-label {{ display: flex; justify-content: space-between; gap: 12px; color: #d7e9f5; }}
    .bar {{ height: 11px; background: rgba(148,184,210,.14); border-radius: 999px; overflow: hidden; }}
    .bar i {{ display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--cyan), var(--gold), var(--coral)); }}
    .bridge {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px; }}
    .bridge span {{ min-height: 86px; display: grid; place-items: center; text-align: center; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: rgba(255,255,255,.04); color: #d8e8f4; font-weight: 750; }}
    .examples {{ display: grid; gap: 14px; }}
    .before-after {{ display: grid; grid-template-columns: minmax(0, .85fr) minmax(0, 1.15fr); gap: 18px; padding: 18px; }}
    .before-after > div:first-child {{ border-right: 1px solid var(--line); padding-right: 18px; }}
    .pill {{ display: inline-flex; width: fit-content; border: 1px solid rgba(56,213,255,.34); background: rgba(56,213,255,.1); color: #bdefff; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; }}
    .gates {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }}
    .gate-card {{ padding: 18px; min-height: 180px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 900px; }}
    th, td {{ text-align: left; padding: 13px 14px; border-bottom: 1px solid rgba(148,184,210,.12); vertical-align: top; }}
    th {{ color: #d9eef8; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; background: rgba(255,255,255,.035); }}
    td {{ color: #c7d8e4; font-size: 14px; }}
    .roadmap {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }}
    .roadmap-card {{ padding: 16px; min-height: 126px; }}
    .roadmap-card span {{ color: var(--gold); font-weight: 900; }}
    footer {{ padding: 36px 0 54px; color: var(--muted); border-top: 1px solid var(--line); }}
    @media (max-width: 1040px) {{
      .hero, .grid-2 {{ grid-template-columns: 1fr; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .gates, .roadmap {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 640px) {{
      .shell {{ width: min(100% - 24px, 1180px); }}
      .nav-links {{ display: none; }}
      .hero {{ padding-top: 48px; }}
      .metrics, .gates, .roadmap, .bridge {{ grid-template-columns: 1fr; }}
      .funnel-row, .status-row, .before-after {{ grid-template-columns: 1fr; }}
      .before-after > div:first-child {{ border-right: 0; border-bottom: 1px solid var(--line); padding-right: 0; padding-bottom: 16px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="shell">
      <nav>
        <div class="brand">{esc(PROJECT_NAME)}</div>
        <div class="nav-links">
          <a href="#engine">Engine</a>
          <a href="#command-center">Command Center</a>
          <a href="#quality">Quality Gates</a>
          <a href="#roadmap">Roadmap</a>
        </div>
      </nav>
    </div>
  </header>

  <main>
    <section class="hero shell">
      <div>
        <span class="eyebrow">Metadata intelligence for anime libraries</span>
        <h1>Stop browsing empty episode pages.</h1>
        <p class="lead">{esc(PROJECT_NAME)} turns incomplete TVDB anime records into localized, review-ready metadata through a controlled recovery pipeline.</p>
        <div class="hero-actions">
          <a class="button primary" href="#command-center">View Showcase</a>
          <a class="button" href="../scripts/generate_metadata_command_center.py">Run Demo</a>
          <a class="button" href="./OPERATING_ROADMAP.md">Roadmap</a>
        </div>
      </div>
      <div class="hero-visual">
        <img src="./assets/metadata_command_surface.svg" alt="Abstract metadata reconstruction command surface">
      </div>
    </section>

    <section class="shell" id="engine">
      <div class="section-head">
        <div>
          <span class="eyebrow">The Recovery System</span>
          <h2>A multi-stage control system for metadata chaos.</h2>
        </div>
        <p>Generic translation turns one string into another. AniMeta Nexus deploys Signal Acquisition, Placeholder Suppression, Continuity-Aware Context Assembly, Semantic Reconstruction, Review Governance, and Contribution-Ready Distribution.</p>
      </div>
      <img src="./assets/recovery_workflow_rail.svg" alt="Discovery to export metadata workflow" style="width:100%;height:auto;border-radius:12px;border:1px solid var(--line);">
    </section>

    <section class="shell" id="command-center">
      <div class="section-head">
        <div>
          <span class="eyebrow">Metadata Command Center</span>
          <h2>Operational visibility for the recovery domain.</h2>
        </div>
        <p>Public demo data is synthetic and safe. Runtime queues, credentials, browser sessions, and private processing logs stay local.</p>
      </div>
      <div class="metrics">
        {render_metric_cards(metrics)}
      </div>
    </section>

    <section class="shell">
      <div class="grid-2">
        <article class="panel">
          <span class="eyebrow">Recovery Funnel</span>
          <h2>From metadata voids to review-ready records.</h2>
          {render_funnel(report['funnel'])}
        </article>
        <article class="panel">
          <span class="eyebrow">Status Distribution</span>
          <h2>Controlled lifecycle, visible state.</h2>
          {render_status_distribution(report['status_distribution'])}
        </article>
      </div>
    </section>

    <section class="shell">
      <div class="grid-2">
        <article class="panel">
          <span class="eyebrow">Placeholder Elimination</span>
          <h2>TBA is not metadata.</h2>
          <p>The Placeholder Suppression Firewall rejects metadata debris before it contaminates the reconstruction layer and becomes polished nonsense.</p>
          <div class="bridge">
            <span>TBA / TBD</span>
            <span>empty fields</span>
            <span>numeric-only titles</span>
            <span>weak source text</span>
          </div>
        </article>
        <article class="panel">
          <span class="eyebrow">Language Bridge</span>
          <h2>{esc(report['language_bridge']['demo_target'])}</h2>
          <p>The Localization Policy Engine keeps the demo in English while preserving the architecture for configurable target-language metadata policy.</p>
          <div class="bridge">
            <span>Source Signal</span>
            <span>Context Pack</span>
            <span>Reconstruction Core</span>
            <span>Target Metadata</span>
          </div>
        </article>
      </div>
    </section>

    <section class="shell">
      <div class="section-head">
        <div>
          <span class="eyebrow">Before / After</span>
          <h2>Blank cards become recovered episode intelligence.</h2>
        </div>
      </div>
      <div class="examples">
        {render_examples(report['before_after_examples'])}
      </div>
    </section>

    <section class="shell" id="quality">
      <div class="section-head">
        <div>
          <span class="eyebrow">Quality Gates</span>
          <h2>Named gates for each domain of metadata failure.</h2>
        </div>
      </div>
      <div class="gates">
        {render_quality_gates(report['quality_gates'])}
      </div>
    </section>

    <section class="shell">
      <div class="section-head">
        <div>
          <span class="eyebrow">Demo Records</span>
          <h2>Review surface sample</h2>
        </div>
      </div>
      {render_records(report['recent_records'])}
    </section>

    <section class="shell" id="roadmap">
      <div class="section-head">
        <div>
          <span class="eyebrow">Roadmap</span>
          <h2>From showcase to operational metadata layer.</h2>
        </div>
      </div>
      <div class="roadmap">
        {render_roadmap(report['roadmap'])}
      </div>
    </section>
  </main>

  <footer>
    <div class="shell">
      {esc(TAGLINE)} Generated from deterministic demo data at {esc(report['generated_at'])}.
    </div>
  </footer>
</body>
</html>
"""


def generate(*, refresh_demo_data: bool = False) -> dict[str, Any]:
    seed_demo_files(refresh=refresh_demo_data)
    inputs, outputs = load_demo()
    report = make_report(inputs, outputs)
    write_json(COMMAND_CENTER_REPORT, report)
    write_assets()
    INDEX_HTML.write_text(render_html(report), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the AniMeta Nexus static showcase.")
    parser.add_argument(
        "--refresh-demo-data",
        action="store_true",
        help="Rewrite the synthetic sample input/output corpus before generating the report.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = generate(refresh_demo_data=args.refresh_demo_data)
    print(f"Generated {COMMAND_CENTER_REPORT.relative_to(ROOT)}")
    print(f"Generated {INDEX_HTML.relative_to(ROOT)}")
    print(f"Demo records: {report['metrics']['demo_corpus_episodes']}")


if __name__ == "__main__":
    main()
