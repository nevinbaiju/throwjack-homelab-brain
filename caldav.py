"""CalDAV client. Replaces ha.py as the phone-facing surface.

Deliberately hand-rolled rather than pulling in a CalDAV library: a VTODO is a
dozen lines, we control both ends, and the failure modes of someone else's
abstraction over iCalendar are harder to debug than the format itself.

Identity is the VTODO UID, which we set to the task id. Under CalDAV the UID is
the resource identity and survives edits on the phone, so there is no footer to
parse and no uid churn — the two things that made the HA projection fragile.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("CALDAV_URL", "http://127.0.0.1:5232").rstrip("/")
USER = os.environ.get("CALDAV_USER", "admin")
PASSWORD = os.environ.get("CALDAV_PASSWORD", "")

PRODID = "-//focus-board-brain//EN"


class CalDavError(RuntimeError):
    pass


def _url(collection: str, path: str = "") -> str:
    return f"{BASE}/{USER}/{collection}/{path}"


def _request(method: str, url: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("User-Agent", "focus-board-brain/0.1")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        if e.code in (207, 404, 412):                 # meaningful, not failures
            return e.code, dict(e.headers), e.read()
        raise CalDavError(f"{method} {url} -> {e.code}: {e.read()[:200].decode(errors='replace')}") from None
    except Exception as e:
        raise CalDavError(f"{method} {url}: {type(e).__name__}: {e}") from None


# --- iCalendar text handling ----------------------------------------------

def _esc(value: str) -> str:
    """RFC 5545 TEXT escaping. Order matters: backslash first."""
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _unesc(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _fold(line: str) -> str:
    """Lines over 75 octets must be folded or strict parsers reject them."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, start = [], 0
    while start < len(raw):
        end = min(start + (75 if not chunks else 74), len(raw))
        # Back off while the FIRST EXCLUDED byte is a UTF-8 continuation byte:
        # that means the split would cut a multibyte character in half.
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
    return "\r\n ".join(chunks)


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_vtodo(uid: str, summary: str, *, due: datetime | None = None,
                alarm_at: datetime | None = None, description: str = "",
                priority: int | None = None, completed: bool = False) -> str:
    now = _utc(datetime.now(timezone.utc))
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "BEGIN:VTODO",
           f"UID:{uid}", f"DTSTAMP:{now}", _fold(f"SUMMARY:{_esc(summary)}")]
    if description:
        out.append(_fold(f"DESCRIPTION:{_esc(description)}"))
    if due:
        out.append(f"DUE:{_utc(due)}")
    if priority is not None:
        out.append(f"PRIORITY:{priority}")
    out.append(f"STATUS:{'COMPLETED' if completed else 'NEEDS-ACTION'}")
    if completed:
        out.append(f"COMPLETED:{now}")
        out.append("PERCENT-COMPLETE:100")
    if alarm_at and not completed:
        # Absolute trigger, never an offset: the brain has already applied
        # quiet hours, and an offset would let the phone recompute past them.
        out += ["BEGIN:VALARM", f"UID:{uid}-alarm", "ACTION:DISPLAY",
                f"TRIGGER;VALUE=DATE-TIME:{_utc(alarm_at)}",
                _fold(f"DESCRIPTION:{_esc(summary)}"), "END:VALARM"]
    out += ["END:VTODO", "END:VCALENDAR"]
    return "\r\n".join(out) + "\r\n"


def parse_vtodo(text: str) -> dict:
    """Pull back what the brain acts on, plus what it needs to verify.

    PRIORITY, DUE and VALARM used to be discarded here. That made divergence
    between the database and the board undetectable: a push that failed left a
    stale card and nothing could see it, because the only view of a card
    omitted every field worth comparing. They are kept now so board.audit()
    can diff the two.
    """
    result: dict = {"uid": None, "summary": "", "status": "NEEDS-ACTION",
                    "completed_at": None, "description": "",
                    "priority": None, "due": None,
                    "has_alarm": False, "alarm_trigger": None}
    in_alarm = False
    for line in _unfold(text):
        if line.startswith("BEGIN:VALARM"):
            in_alarm = True
            result["has_alarm"] = True
            continue
        if line.startswith("END:VALARM"):
            in_alarm = False
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        key = name.split(";")[0].upper()
        if in_alarm:
            # Only the trigger matters; the rest of the alarm is boilerplate.
            if key == "TRIGGER":
                result["alarm_trigger"] = value.strip()
            continue
        if key == "UID":
            result["uid"] = value.strip()
        elif key == "SUMMARY":
            result["summary"] = _unesc(value)
        elif key == "DESCRIPTION":
            result["description"] = _unesc(value)
        elif key == "STATUS":
            result["status"] = value.strip().upper()
        elif key == "COMPLETED":
            result["completed_at"] = value.strip()
        elif key == "PRIORITY":
            try:
                result["priority"] = int(value.strip())
            except ValueError:
                pass
        elif key == "DUE":
            result["due"] = value.strip()
    return result


# --- CalDAV operations ----------------------------------------------------

def ensure_collection(collection: str, display_name: str | None = None) -> None:
    status, _, _ = _request("PROPFIND", _url(collection), headers={"Depth": "0"})
    if status in (200, 207):
        return
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<create xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><set><prop>'
            '<resourcetype><collection/><C:calendar/></resourcetype>'
            '<C:supported-calendar-component-set><C:comp name="VTODO"/></C:supported-calendar-component-set>'
            f'<displayname>{display_name or collection}</displayname>'
            '</prop></set></create>').encode()
    code, _, _ = _request("MKCOL", _url(collection), body, {"Content-Type": "application/xml"})
    if code not in (201, 200):
        raise CalDavError(f"could not create collection {collection}: {code}")


def list_collections() -> list[dict]:
    """Every VTODO collection under the principal, with its display name.

    Needed once shopping lists became things you can add and remove: a
    hardcoded SHOPPING constant cannot describe a list created five minutes
    ago. The server is the truth.
    """
    body = ('<?xml version="1.0"?>'
            '<propfind xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<prop><displayname/><resourcetype/>'
            '<C:supported-calendar-component-set/></prop></propfind>').encode()
    _, _, data = _request("PROPFIND", f"{BASE}/{USER}/", body,
                          {"Depth": "1", "Content-Type": "application/xml"})
    text = data.decode("utf-8", errors="replace")
    out = []
    for block in re.findall(r"<[^>]*response[^>]*>(.*?)</[^>]*response>", text, re.S):
        href = re.search(r"<[^>]*href[^>]*>([^<]*)<", block)
        if not href:
            continue
        name = urllib.parse.unquote(href.group(1)).rstrip("/").rsplit("/", 1)[-1]
        if not name or name == USER:
            continue
        # Only VTODO collections; a calendar of events is not a task list.
        if "VTODO" not in block.upper():
            continue
        disp = re.search(r"<[^>]*displayname[^>]*>([^<]*)<", block)
        out.append({"collection": name,
                    "label": (disp.group(1).strip() if disp else "") or name})
    return sorted(out, key=lambda c: c["collection"])


def delete_collection(collection: str) -> None:
    """Remove a whole collection and everything in it. There is no undo."""
    code, _, _ = _request("DELETE", _url(collection))
    if code not in (200, 204, 404):
        raise CalDavError(f"could not delete collection {collection}: {code}")


def get_ctag(collection: str) -> str | None:
    """Collection-wide change tag. Unchanged means skip the whole poll."""
    body = ('<?xml version="1.0"?><propfind xmlns="DAV:" xmlns:CS="http://calendarserver.org/ns/">'
            '<prop><CS:getctag/></prop></propfind>').encode()
    _, _, data = _request("PROPFIND", _url(collection), body, {"Depth": "0", "Content-Type": "application/xml"})
    m = re.search(rb"<[^>]*getctag[^>]*>([^<]*)<", data)
    return m.group(1).decode() if m else None


def list_etags(collection: str) -> dict[str, str]:
    """-> {uid: etag} for everything in the collection."""
    body = ('<?xml version="1.0"?><propfind xmlns="DAV:"><prop><getetag/></prop></propfind>').encode()
    _, _, data = _request("PROPFIND", _url(collection), body, {"Depth": "1", "Content-Type": "application/xml"})
    out: dict[str, str] = {}
    for block in re.findall(rb"<[^>]*response[^>]*>(.*?)</[^>]*response>", data, re.S):
        href = re.search(rb"<[^>]*href[^>]*>([^<]*)<", block)
        etag = re.search(rb"<[^>]*getetag[^>]*>([^<]*)<", block)
        if not href or not etag:
            continue
        name = href.group(1).decode().rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".ics"):
            out[name[:-4]] = etag.group(1).decode().strip('"')
    return out


def put_todo(collection: str, uid: str, ics: str, etag: str | None = None) -> str | None:
    headers = {"Content-Type": "text/calendar; charset=utf-8"}
    if etag:
        headers["If-Match"] = f'"{etag}"'          # lose the race, do not clobber
    code, resp, _ = _request("PUT", _url(collection, f"{uid}.ics"), ics.encode("utf-8"), headers)
    if code == 412:
        raise CalDavError(f"{uid}: changed on the phone since we last read it")
    return (resp.get("ETag") or "").strip('"') or None


def get_todo(collection: str, uid: str) -> dict | None:
    code, _, data = _request("GET", _url(collection, f"{uid}.ics"))
    if code == 404:
        return None
    return parse_vtodo(data.decode("utf-8", errors="replace"))


def delete_todo(collection: str, uid: str) -> None:
    _request("DELETE", _url(collection, f"{uid}.ics"))


def list_todos(collection: str) -> list[dict]:
    """Every VTODO in a collection, parsed. One multiget instead of N GETs."""
    body = ('<?xml version="1.0"?>'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<D:prop><D:getetag/><C:calendar-data/></D:prop>'
            '<C:filter><C:comp-filter name="VCALENDAR">'
            '<C:comp-filter name="VTODO"/></C:comp-filter></C:filter>'
            '</C:calendar-query>').encode()
    _, _, data = _request("REPORT", _url(collection), body,
                          {"Depth": "1", "Content-Type": "application/xml"})
    text = data.decode("utf-8", errors="replace")
    out = []
    for block in re.findall(r"<[^>]*response[^>]*>(.*?)</[^>]*response>", text, re.S):
        etag = re.search(r"<[^>]*getetag[^>]*>([^<]*)<", block)
        cal = re.search(r"<[^>]*calendar-data[^>]*>(.*?)</[^>]*calendar-data>", block, re.S)
        if not cal:
            continue
        raw = (cal.group(1).replace("&lt;", "<").replace("&gt;", ">")
               .replace("&quot;", '"').replace("&amp;", "&"))
        item = parse_vtodo(raw)
        if item.get("uid"):
            item["etag"] = etag.group(1).strip('"') if etag else None
            out.append(item)
    return out
