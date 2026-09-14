"""LiteLLM client with hard schema validation.

The rule from the proposal: the model PROPOSES, code DECIDES. Nothing a model
returns reaches the CalDAV board without passing through validate() first.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

TRACKS = {"meta", "focus", "flow", "waiting", "upkeep", "someday"}
CONSEQUENCES = {"breaks", "costs", "improves", "optional"}

# Meta items you have not prioritised must not sink to the bottom of the lane,
# so an unset work priority lands mid-scale rather than at zero.
META_DEFAULT_CONSEQUENCE = "costs"

# You type these; Python parses them, not the model.
PRIORITY_MARKERS = {
    "p1": "breaks", "p2": "costs", "p3": "improves", "p4": "optional",
    "!": "breaks", "!!": "breaks",
}
_MARKER_RE = re.compile(r"^\s*(p[1-4]|!{1,2})\s*[:\-]?\s+", re.IGNORECASE)

# A leading "w:" or a work source means Meta. Decided here, in Python — asking
# the model to infer "is this work?" from deliberately opaque shorthand is
# exactly the judgement it cannot make.
_WORK_PREFIX_RE = re.compile(r"^\s*w\s*[:\-]\s*", re.IGNORECASE)
WORK_SOURCES = {"shortcut-work", "shortcut-task-work", "work"}


class LLMError(RuntimeError):
    pass


def complete(alias: str, system: str, user: str, *, max_tokens: int = 4096,
             temperature: float = 0.0, timeout: int = 120) -> tuple[str, str, int]:
    """-> (content, model_actually_used, elapsed_ms)"""
    body = {
        "model": alias,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{LITELLM_URL}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {MASTER_KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "focus-board-brain/0.1"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"{alias}: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
    except Exception as e:
        raise LLMError(f"{alias}: {type(e).__name__}: {e}") from None

    ms = int((time.time() - t0) * 1000)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise LLMError(f"{alias}: no content in response: {json.dumps(data)[:200]}") from None
    return content, data.get("model", alias), ms


def _loads(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):                      # some models fence anyway
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text[4:] if text.lower().startswith("json") else text
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise LLMError(f"not valid JSON: {e}") from None


def coerce_minutes(value) -> int | None:
    """Accept 60, 60.0, "60", "60m", "1h" -> 60. Reject bools and nonsense."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        text = str(value).strip().lower()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hrs|hours)?", text)
        if not m:
            return None
        n = int(float(m.group(1)) * (60 if (m.group(2) or "m").startswith("h") else 1))
    return n if 1 <= n <= 1440 else None


def strip_priority_marker(text: str) -> tuple[str, str | None]:
    """'p1: rebase qz-7' -> ('rebase qz-7', 'breaks'). Pure Python, no model."""
    m = _MARKER_RE.match(text or "")
    if not m:
        return text, None
    return text[m.end():].strip(), PRIORITY_MARKERS[m.group(1).lower()]


def route(capture: dict) -> tuple[bool, str | None, str]:
    """Deterministic routing from the capture itself.

    -> (is_meta, consequence_from_marker, text_with_markers_stripped)

    A priority marker is itself a work signal: you only reach for `p2:` when
    you are logging something on your own plate and setting its priority.
    """
    text = capture.get("text", "")
    is_meta = capture.get("source") in WORK_SOURCES

    m = _WORK_PREFIX_RE.match(text)
    if m:
        is_meta, text = True, text[m.end():].strip()

    text, marker = strip_priority_marker(text)
    if marker:
        is_meta = True

    # A bare "w:" with nothing after it should not become an empty card.
    return is_meta, marker, (text.strip() or capture.get("text", "").strip())


def validate_triage(raw: str, captures: list[dict]) -> list[dict]:
    """Coerce and check a triage response. Raises LLMError if unusable.

    Anything the model got wrong that we can safely default, we default; anything
    that would corrupt the board, we reject.
    """
    data = _loads(raw)
    items = data.get("items")
    if not isinstance(items, list):
        raise LLMError("response has no 'items' list")

    by_id = {c["id"]: c for c in captures}
    seen: set[str] = set()
    out: list[dict] = []

    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        cid = raw_item.get("capture_id")
        if cid not in by_id or cid in seen:
            continue                                # hallucinated or duplicated
        seen.add(cid)
        capture = by_id[cid]

        track = str(raw_item.get("track", "")).lower().strip()
        if track not in TRACKS:
            track = "flow"                          # safest lane: visible, ages, uncapped

        title = (raw_item.get("title") or "").strip()
        consequence = raw_item.get("consequence")
        consequence = str(consequence).lower().strip() if consequence else None

        is_meta, marker, cleaned = route(capture)
        if is_meta or track == "meta":
            # Never let a model reinterpret your shorthand. Verbatim, always.
            track, title = "meta", cleaned
            consequence = marker or META_DEFAULT_CONSEQUENCE
        else:
            if not title:
                title = capture["text"].strip()
            if consequence not in CONSEQUENCES:
                consequence = "improves"

        estimate = coerce_minutes(raw_item.get("estimate_min"))

        due = raw_item.get("due")
        if not (isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)):
            due = None

        project = raw_item.get("project")
        project = re.sub(r"[^a-z0-9_-]", "", str(project).lower())[:40] if project else None

        blocked_on = raw_item.get("blocked_on")
        blocked_on = str(blocked_on).strip()[:60] if blocked_on else None

        out.append({
            "capture_id": cid, "track": track, "title": title[:255],
            "project": project or None, "consequence": consequence,
            "estimate_min": estimate, "due": due, "blocked_on": blocked_on,
            "note": capture["text"] if title != capture["text"] else "",
        })

    if not out:
        raise LLMError(f"no usable items for {len(captures)} captures")
    return out
