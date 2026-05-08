#!/usr/bin/env python3
"""Combine ICS and Google Calendar sources into generated ICS feeds.

This utility intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import dataclasses
import datetime as dt
import gzip
import hashlib
import hmac
import http.server
import json
import os
import pathlib
import secrets
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any


GOOGLE_CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_CONFIG_PATH = "calendars.toml"
DEFAULT_OUTPUT_PATH = "combined.ics"
USER_AGENT = "ics-combine/1.0"


class ConfigError(Exception):
    """Raised when configuration is invalid."""


class FetchError(Exception):
    """Raised when a source cannot be fetched or parsed."""


@dataclasses.dataclass
class IcsComponent:
    name: str
    properties: list[str]
    children: list["IcsComponent"]

    def clone(self) -> "IcsComponent":
        return IcsComponent(
            self.name,
            list(self.properties),
            [child.clone() for child in self.children],
        )


@dataclasses.dataclass
class SourceResult:
    source_id: str
    name: str
    color: str
    events: list[IcsComponent]
    timezones: list[IcsComponent]


@dataclasses.dataclass
class OutputSpec:
    name: str
    file: str
    s3_key: str | None
    include_source_ids: list[str] | None
    skip_description: bool = False
    skip_attendees: bool = False


@dataclasses.dataclass
class AwsCredentials:
    access_key: str
    secret_key: str
    session_token: str | None = None


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def load_config(path: str | pathlib.Path) -> dict[str, Any]:
    config_path = pathlib.Path(path)
    if not config_path.exists():
        raise ConfigError(
            f"Config file {config_path} does not exist. Run `make init-config` first."
        )
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    validate_config(data)
    return data


def validate_config(config: dict[str, Any]) -> None:
    calendars = config.get("calendars")
    if not isinstance(calendars, list) or not calendars:
        raise ConfigError("Config must define at least one [[calendars]] source.")

    seen_ids: set[str] = set()
    for index, source in enumerate(calendars, start=1):
        if not isinstance(source, dict):
            raise ConfigError(f"Calendar source #{index} must be a table.")
        source_id = require_string(source, "id", f"calendar source #{index}")
        if source_id in seen_ids:
            raise ConfigError(f"Duplicate calendar source id: {source_id}")
        seen_ids.add(source_id)
        require_string(source, "name", f"calendar source {source_id}")
        require_string(source, "color", f"calendar source {source_id}")
        source_type = require_string(source, "type", f"calendar source {source_id}")
        if source_type == "ics":
            require_string(source, "url", f"calendar source {source_id}")
        elif source_type == "google":
            require_string(source, "calendar_id", f"calendar source {source_id}")
        else:
            raise ConfigError(
                f"Calendar source {source_id} has unsupported type {source_type!r}."
            )

    outputs = config.get("outputs", [])
    if outputs is None:
        outputs = []
    if not isinstance(outputs, list):
        raise ConfigError("[[outputs]] must be an array of tables.")
    for index, output in enumerate(outputs, start=1):
        if not isinstance(output, dict):
            raise ConfigError(f"Output #{index} must be a table.")
        require_string(output, "name", f"output #{index}")
        require_string(output, "file", f"output #{index}")
        if "exclude_source_id" in output:
            raise ConfigError(
                f"Output #{index} uses removed field exclude_source_id; "
                "use include_source_ids instead."
            )
        include_source_ids = output.get("include_source_ids")
        if not isinstance(include_source_ids, list) or not include_source_ids:
            raise ConfigError(
                f"Output #{index} must define non-empty include_source_ids array."
            )
        for source_id in include_source_ids:
            if not isinstance(source_id, str) or not source_id:
                raise ConfigError(
                    f"Output #{index} include_source_ids must contain only strings."
                )
            if source_id not in seen_ids:
                raise ConfigError(
                    f"Output #{index} includes unknown source id {source_id!r}."
                )
        s3_key = output.get("s3_key")
        if s3_key is not None and (not isinstance(s3_key, str) or not s3_key):
            raise ConfigError(f"Output #{index} s3_key must be a non-empty string.")
        skip_description = output.get("skip_description")
        if skip_description is not None and not isinstance(skip_description, bool):
            raise ConfigError(f"Output #{index} skip_description must be a boolean.")
        skip_attendees = output.get("skip_attendees")
        if skip_attendees is not None and not isinstance(skip_attendees, bool):
            raise ConfigError(f"Output #{index} skip_attendees must be a boolean.")

    s3_config = config.get("s3")
    if s3_config is not None:
        if not isinstance(s3_config, dict):
            raise ConfigError("[s3] must be a table.")
        s3_gzip_enabled(s3_config)


def require_string(table: dict[str, Any], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{context} must define non-empty string {key!r}.")
    return value


def s3_gzip_enabled(s3_config: dict[str, Any]) -> bool:
    value = s3_config.get("gzip")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ConfigError("[s3].gzip must be a boolean if present.")
    return value


def calendar_url_candidates(url: str) -> list[str]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme == "webcals":
        return [urllib.parse.urlunsplit(("https", *parsed[1:]))]
    if scheme == "webcal":
        return [
            urllib.parse.urlunsplit(("https", *parsed[1:])),
            urllib.parse.urlunsplit(("http", *parsed[1:])),
        ]
    return [url]


def read_text_response(url: str, timeout: int = 30) -> str:
    last_error: Exception | None = None
    candidates = calendar_url_candidates(url)
    for candidate_url in candidates:
        try:
            request = urllib.request.Request(
                candidate_url, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
            break
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
    else:
        attempted = ", ".join(candidates)
        raise FetchError(f"Unable to fetch {url}; attempted {attempted}: {last_error}")

    encoding = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            encoding = part.split("=", 1)[1].strip()
            break
    return raw.decode(encoding, errors="replace")


def unfold_ics_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    lines: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_ics(text: str) -> IcsComponent:
    lines = [line for line in unfold_ics_lines(text) if line]
    if not lines:
        raise FetchError("ICS file is empty.")
    root, next_index = parse_component(lines, 0)
    if root.name != "VCALENDAR":
        raise FetchError("ICS root component must be VCALENDAR.")
    if next_index != len(lines):
        raise FetchError("Unexpected content after VCALENDAR.")
    return root


def parse_component(lines: list[str], index: int) -> tuple[IcsComponent, int]:
    begin = lines[index]
    if not begin.upper().startswith("BEGIN:"):
        raise FetchError(f"Expected BEGIN line, got {begin!r}.")
    name = begin.split(":", 1)[1].strip().upper()
    properties: list[str] = []
    children: list[IcsComponent] = []
    index += 1

    while index < len(lines):
        line = lines[index]
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            child, index = parse_component(lines, index)
            children.append(child)
            continue
        if upper == f"END:{name}":
            return IcsComponent(name, properties, children), index + 1
        if upper.startswith("END:"):
            raise FetchError(f"Mismatched component end {line!r}; expected END:{name}.")
        properties.append(line)
        index += 1

    raise FetchError(f"Missing END:{name}.")


def component_to_unfolded_lines(component: IcsComponent) -> list[str]:
    lines = [f"BEGIN:{component.name}"]
    lines.extend(component.properties)
    for child in component.children:
        lines.extend(component_to_unfolded_lines(child))
    lines.append(f"END:{component.name}")
    return lines


def serialize_calendar(component: IcsComponent) -> str:
    folded: list[str] = []
    for line in component_to_unfolded_lines(component):
        folded.extend(fold_ics_line(line))
    return "\r\n".join(folded) + "\r\n"


def fold_ics_line(line: str, limit: int = 75) -> list[str]:
    chunks: list[str] = []
    current = ""
    current_limit = limit
    for char in line:
        candidate = current + char
        if current and len(candidate.encode("utf-8")) > current_limit:
            chunks.append(current)
            current = char
            current_limit = limit - 1
        else:
            current = candidate
    chunks.append(current)
    if len(chunks) == 1:
        return chunks
    return [chunks[0], *[" " + chunk for chunk in chunks[1:]]]


def split_property_line(line: str) -> tuple[str, str]:
    in_quote = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if char == ":" and not in_quote:
            return line[:index], line[index + 1 :]
    return line, ""


def property_name(line: str) -> str:
    prefix, _ = split_property_line(line)
    name_part = prefix.split(";", 1)[0]
    if "." in name_part:
        name_part = name_part.rsplit(".", 1)[1]
    return name_part.upper()


def ics_unescape_text(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            result.append(char)
            index += 1
            continue
        escaped = value[index + 1]
        if escaped in ("n", "N"):
            result.append("\n")
        elif escaped in ("\\", ";", ","):
            result.append(escaped)
        else:
            result.append(escaped)
        index += 2
    return "".join(result)


def ics_escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def quote_param(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if any(char in escaped for char in (":", ";", ",", " ")):
        return f'"{escaped}"'
    return escaped


def transform_event(
    event: IcsComponent,
    source_name: str,
    color: str,
    skip_description: bool = False,
    skip_attendees: bool = False,
) -> IcsComponent:
    transformed = event.clone()
    transformed.properties = [
        prop
        for prop in transformed.properties
        if property_name(prop) != "COLOR"
        and (not skip_description or property_name(prop) != "DESCRIPTION")
        and (not skip_attendees or property_name(prop) != "ATTENDEE")
    ]
    if not skip_description:
        append_source_to_description(transformed, source_name)
    transformed.properties.append(f"COLOR:{ics_escape_text(color)}")
    return transformed


def append_source_to_description(event: IcsComponent, source_name: str) -> None:
    suffix = f"Source calendar: {source_name}"
    for index, prop in enumerate(event.properties):
        if property_name(prop) != "DESCRIPTION":
            continue
        prefix, value = split_property_line(prop)
        description = ics_unescape_text(value)
        if suffix not in description:
            separator = "\n\n" if description else ""
            description = f"{description}{separator}{suffix}"
        event.properties[index] = f"{prefix}:{ics_escape_text(description)}"
        return
    event.properties.append(f"DESCRIPTION:{ics_escape_text(suffix)}")


def component_property_value(component: IcsComponent, name: str) -> str | None:
    wanted = name.upper()
    for prop in component.properties:
        if property_name(prop) == wanted:
            _, value = split_property_line(prop)
            return value
    return None


def source_window(source: dict[str, Any]) -> tuple[dt.date | None, dt.date | None]:
    today = dt.datetime.now(dt.timezone.utc).date()
    start: dt.date | None = None
    end: dt.date | None = None
    if "include_past_days" in source:
        days = int(source["include_past_days"])
        if days < 0:
            raise ConfigError(f"Source {source['id']} include_past_days cannot be negative.")
        start = today - dt.timedelta(days=days)
    if "include_future_days" in source:
        days = int(source["include_future_days"])
        if days < 0:
            raise ConfigError(f"Source {source['id']} include_future_days cannot be negative.")
        end = today + dt.timedelta(days=days)
    return start, end


def filter_events_for_source_window(
    events: list[IcsComponent], source: dict[str, Any]
) -> list[IcsComponent]:
    start, end = source_window(source)
    if start is None and end is None:
        return events
    return [event for event in events if event_in_window(event, start, end)]


def filter_events_for_source_options(
    events: list[IcsComponent], source: dict[str, Any]
) -> list[IcsComponent]:
    filtered = filter_events_for_source_window(events, source)
    if source.get("exclude_cancelled"):
        filtered = [event for event in filtered if not event_is_cancelled(event)]
    return filtered


def event_is_cancelled(event: IcsComponent) -> bool:
    for prop in event.properties:
        if property_name(prop) == "STATUS":
            _, value = split_property_line(prop)
            return value.strip().upper() == "CANCELLED"
    return False


def event_in_window(
    event: IcsComponent, start: dt.date | None, end: dt.date | None
) -> bool:
    event_start = event_start_date(event)
    if recurring_event_has_not_ended(event, start):
        return True
    if event_start is None:
        return True
    if start is not None and event_start < start:
        return False
    if end is not None and event_start > end:
        return False
    return True


def event_start_date(event: IcsComponent) -> dt.date | None:
    value = component_property_value(event, "DTSTART")
    if not value or len(value) < 8 or not value[:8].isdigit():
        return None
    try:
        return dt.datetime.strptime(value[:8], "%Y%m%d").date()
    except ValueError:
        return None


def recurring_event_has_not_ended(event: IcsComponent, start: dt.date | None) -> bool:
    rrules = [
        split_property_line(prop)[1]
        for prop in event.properties
        if property_name(prop) == "RRULE"
    ]
    if not rrules:
        return False
    if start is None:
        return True
    for rrule in rrules:
        until = rrule_until_date(rrule)
        if until is None or until >= start:
            return True
    return False


def rrule_until_date(rrule: str) -> dt.date | None:
    for part in rrule.split(";"):
        if not part.startswith("UNTIL="):
            continue
        value = part.split("=", 1)[1]
        if len(value) < 8 or not value[:8].isdigit():
            return None
        try:
            return dt.datetime.strptime(value[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def extract_ics_source(source: dict[str, Any]) -> SourceResult:
    text = read_text_response(source["url"], int(source.get("timeout_seconds", 30)))
    root = parse_ics(text)
    events = filter_events_for_source_options(
        [child for child in root.children if child.name == "VEVENT"],
        source,
    )
    timezones = [child for child in root.children if child.name == "VTIMEZONE"]
    return SourceResult(
        source_id=source["id"],
        name=source["name"],
        color=source["color"],
        events=events,
        timezones=timezones,
    )


def fetch_all_sources(
    config: dict[str, Any], config_path: pathlib.Path | None = None
) -> tuple[list[SourceResult], list[str]]:
    results: list[SourceResult] = []
    warnings: list[str] = []
    total_sources = len(config["calendars"])
    for index, source in enumerate(config["calendars"], start=1):
        print(
            f"Processing input {index}/{total_sources}: "
            f"{source['id']} ({source['name']}, {source['type']})...",
            flush=True,
        )
        try:
            if source["type"] == "ics":
                result = extract_ics_source(source)
            elif source["type"] == "google":
                result = extract_google_source(config, source, config_path)
            else:
                raise FetchError(f"Unsupported source type {source['type']!r}.")
            results.append(result)
            print(
                f"Finished input {index}/{total_sources}: "
                f"{source['id']} ({source['name']}): "
                f"{format_count(len(result.events), 'calendar item')}",
                flush=True,
            )
        except Exception as exc:
            warnings.append(f"WARNING: skipped source {source['id']} ({source['name']}): {exc}")
    return results, warnings


def format_count(count: int, label: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {label}{suffix}"


def format_kb(byte_count: int) -> str:
    return f"{byte_count / 1024:.1f} KB"


def output_specs(config: dict[str, Any], output_override: str | None = None) -> list[OutputSpec]:
    configured_outputs = config.get("outputs") or []
    if configured_outputs:
        return [
            OutputSpec(
                name=output["name"],
                file=output["file"],
                s3_key=output.get("s3_key"),
                include_source_ids=list(output["include_source_ids"]),
                skip_description=output.get("skip_description", False),
                skip_attendees=output.get("skip_attendees", False),
            )
            for output in configured_outputs
        ]

    s3_config = config.get("s3") or {}
    return [
        OutputSpec(
            name="Combined Calendar",
            file=output_override or DEFAULT_OUTPUT_PATH,
            s3_key=s3_config.get("key"),
            include_source_ids=None,
            skip_description=False,
            skip_attendees=False,
        )
    ]


def calendar_timezone(config: dict[str, Any]) -> str | None:
    calendar_config = config.get("calendar") or {}
    if not isinstance(calendar_config, dict):
        raise ConfigError("[calendar] must be a table.")
    timezone = calendar_config.get("timezone")
    if timezone is None:
        return None
    if not isinstance(timezone, str) or not timezone:
        raise ConfigError("[calendar].timezone must be a non-empty string.")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f"[calendar].timezone is not a valid IANA timezone: {timezone}") from exc
    return timezone


def build_output_calendar(
    output: OutputSpec,
    source_results: list[SourceResult],
    calendar_tz: str | None = None,
) -> IcsComponent:
    if output.include_source_ids is None:
        included_sources = source_results
    else:
        result_by_id = {result.source_id: result for result in source_results}
        included_sources = [
            result_by_id[source_id]
            for source_id in output.include_source_ids
            if source_id in result_by_id
        ]
    if not included_sources:
        raise FetchError(f"Output {output.name!r} has no successful included sources.")

    timezone_by_content: dict[str, IcsComponent] = {}
    timezone_by_tzid: dict[str, IcsComponent] = {}
    events: list[IcsComponent] = []
    for source in included_sources:
        for timezone_component in source.timezones:
            tzid = component_property_value(timezone_component, "TZID")
            if tzid:
                timezone_by_tzid.setdefault(tzid, timezone_component)
            else:
                serialized = "\n".join(component_to_unfolded_lines(timezone_component))
                timezone_by_content.setdefault(serialized, timezone_component)
        for event in source.events:
            events.append(
                transform_event(
                    event,
                    source.name,
                    source.color,
                    skip_description=output.skip_description,
                    skip_attendees=output.skip_attendees,
                )
            )

    properties = [
        "PRODID:-//ics-combine//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"NAME:{ics_escape_text(output.name)}",
        f"X-WR-CALNAME:{ics_escape_text(output.name)}",
        f"LAST-MODIFIED:{format_utc_datetime(dt.datetime.now(dt.timezone.utc))}",
    ]
    if calendar_tz:
        properties.insert(-1, f"X-WR-TIMEZONE:{ics_escape_text(calendar_tz)}")
    children = list(timezone_by_tzid.values()) + list(timezone_by_content.values()) + events
    return IcsComponent("VCALENDAR", properties, children)


def write_outputs(
    config: dict[str, Any],
    source_results: list[SourceResult],
    output_override: str | None = None,
) -> list[tuple[OutputSpec, pathlib.Path]]:
    written: list[tuple[OutputSpec, pathlib.Path]] = []
    timezone = calendar_timezone(config)
    for output in output_specs(config, output_override):
        calendar = build_output_calendar(output, source_results, timezone)
        output_path = pathlib.Path(output.file)
        if output_path.parent != pathlib.Path("."):
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(serialize_calendar(calendar).encode("utf-8"))
        written.append((output, output_path))
    return written


def existing_output_files(
    config: dict[str, Any], output_override: str | None = None
) -> list[tuple[OutputSpec, pathlib.Path]]:
    existing: list[tuple[OutputSpec, pathlib.Path]] = []
    for output in output_specs(config, output_override):
        output_path = pathlib.Path(output.file)
        if not output_path.exists():
            raise FetchError(
                f"Output file {output_path} for {output.name!r} does not exist. "
                "Run `make combine` first."
            )
        existing.append((output, output_path))
    return existing


def output_event_count(output: OutputSpec, source_results: list[SourceResult]) -> int:
    if output.include_source_ids is None:
        return sum(len(source.events) for source in source_results)
    return sum(
        len(source.events)
        for source in source_results
        if source.source_id in set(output.include_source_ids)
    )


def combine_command(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config)
    try:
        config = load_config(config_path)
        source_results, warnings = fetch_all_sources(config, config_path)
        for warning in warnings:
            eprint(warning)
        if not source_results:
            raise FetchError("No calendar sources succeeded.")
        written = write_outputs(config, source_results, args.output)
        for output, path in written:
            item_count = output_event_count(output, source_results)
            size = format_kb(path.stat().st_size)
            print(
                f"Wrote {path} ({output.name}, "
                f"{format_count(item_count, 'calendar item')}, {size})"
            )
        if args.push_s3:
            upload_outputs_to_s3(config, written)
        return 0
    except (ConfigError, FetchError, OSError, urllib.error.URLError) as exc:
        eprint(f"ERROR: {exc}")
        return 1


def upload_command(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config)
    try:
        config = load_config(config_path)
        existing = existing_output_files(config, args.output)
        for output, path in existing:
            print(
                f"Uploading existing {path} "
                f"({output.name}, {format_kb(path.stat().st_size)})"
            )
        upload_outputs_to_s3(config, existing)
        return 0
    except (ConfigError, FetchError, OSError, urllib.error.URLError) as exc:
        eprint(f"ERROR: {exc}")
        return 1


def google_scopes(config: dict[str, Any]) -> list[str]:
    oauth = config.get("google_oauth") or {}
    scopes = oauth.get("scopes") or [GOOGLE_CALENDAR_READONLY_SCOPE]
    if isinstance(scopes, str):
        return [scopes]
    if isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
        return scopes
    raise ConfigError("[google_oauth].scopes must be a string array.")


def require_google_oauth(config: dict[str, Any]) -> dict[str, Any]:
    oauth = config.get("google_oauth")
    if not isinstance(oauth, dict):
        raise ConfigError("Config must define [google_oauth] for Google sources.")
    require_string(oauth, "client_id", "[google_oauth]")
    client_secret = oauth.get("client_secret", "")
    if client_secret is not None and not isinstance(client_secret, str):
        raise ConfigError("[google_oauth].client_secret must be a string if present.")
    return oauth


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_google_auth_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    verifier: str,
    state: str,
) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(query)}"


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    server: "OAuthCallbackServer"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.callback_params = params
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authorization received. You can return to the terminal.")

    def log_message(self, format: str, *args: Any) -> None:
        return


class OAuthCallbackServer(http.server.HTTPServer):
    callback_params: dict[str, list[str]] | None = None


def run_oauth_callback_server(port: int = 0) -> tuple[OAuthCallbackServer, str]:
    server = OAuthCallbackServer(("127.0.0.1", port), OAuthCallbackHandler)
    host, actual_port = server.server_address
    redirect_uri = f"http://{host}:{actual_port}/oauth2callback"
    return server, redirect_uri


def auth_google_command(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config)
    try:
        config = load_config(config_path)
        oauth = require_google_oauth(config)
        scopes = google_scopes(config)
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        server, redirect_uri = run_oauth_callback_server(args.port)
        auth_url = build_google_auth_url(
            oauth["client_id"], redirect_uri, scopes, verifier, state
        )
        print(f"Open this URL to authorize Google Calendar access:\n{auth_url}\n")
        if not args.no_browser:
            webbrowser.open(auth_url)

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        deadline = time.time() + args.timeout_seconds
        while thread.is_alive() and time.time() < deadline:
            time.sleep(0.1)
        server.server_close()
        if server.callback_params is None:
            raise FetchError("Timed out waiting for OAuth callback.")
        params = server.callback_params
        received_state = one_query_value(params, "state")
        if received_state != state:
            raise FetchError("OAuth callback state did not match.")
        if "error" in params:
            raise FetchError(f"Google OAuth error: {one_query_value(params, 'error')}")
        code = one_query_value(params, "code")
        tokens = exchange_google_code(
            oauth=oauth,
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
        merge_google_tokens(config, tokens, scopes)
        save_config(config_path, config)
        print(f"Saved Google OAuth tokens to {config_path}")
        return 0
    except (ConfigError, FetchError, OSError, urllib.error.URLError) as exc:
        eprint(f"ERROR: {exc}")
        return 1


def one_query_value(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key)
    if not values:
        raise FetchError(f"OAuth callback missing {key!r}.")
    return values[0]


def urlopen_json(
    url: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    encoded_data: bytes | None = None
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    if data is not None:
        encoded_data = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        request_headers.setdefault(
            "Content-Type", "application/x-www-form-urlencoded"
        )
    request = urllib.request.Request(
        url, data=encoded_data, method=method, headers=request_headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise FetchError(f"HTTP {exc.code} from {url}: {body}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise FetchError(f"Expected JSON object from {url}.")
    return parsed


def exchange_google_code(
    oauth: dict[str, Any],
    code: str,
    verifier: str,
    redirect_uri: str,
    scopes: list[str],
) -> dict[str, Any]:
    data = {
        "client_id": oauth["client_id"],
        "client_secret": oauth.get("client_secret", ""),
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    response = urlopen_json(GOOGLE_TOKEN_URL, method="POST", data=data)
    if "access_token" not in response:
        raise FetchError("Google token response did not include access_token.")
    response["scopes"] = scopes
    return response


def refresh_google_access_token(oauth: dict[str, Any]) -> dict[str, Any]:
    refresh_token = require_string(oauth, "refresh_token", "[google_oauth]")
    data = {
        "client_id": oauth["client_id"],
        "client_secret": oauth.get("client_secret", ""),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = urlopen_json(GOOGLE_TOKEN_URL, method="POST", data=data)
    if "access_token" not in response:
        raise FetchError("Google refresh response did not include access_token.")
    response.setdefault("refresh_token", refresh_token)
    return response


def merge_google_tokens(
    config: dict[str, Any], tokens: dict[str, Any], scopes: list[str] | None = None
) -> None:
    oauth = config.setdefault("google_oauth", {})
    if not isinstance(oauth, dict):
        raise ConfigError("[google_oauth] must be a table.")
    for key in ("access_token", "refresh_token", "token_type"):
        if key in tokens and tokens[key]:
            oauth[key] = tokens[key]
    if scopes is not None:
        oauth["scopes"] = scopes
    elif tokens.get("scopes"):
        oauth["scopes"] = tokens["scopes"]
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, int | float):
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in)
        oauth["token_expiry"] = isoformat_z(expiry)


def isoformat_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def google_access_token(
    config: dict[str, Any], config_path: pathlib.Path | None = None
) -> str:
    oauth = require_google_oauth(config)
    token = oauth.get("access_token")
    expiry_value = oauth.get("token_expiry")
    if isinstance(token, str) and token and isinstance(expiry_value, str):
        try:
            expiry = parse_rfc3339_datetime(expiry_value)
            if expiry > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60):
                return token
        except ValueError:
            pass
    refreshed = refresh_google_access_token(oauth)
    merge_google_tokens(config, refreshed, google_scopes(config))
    if config_path is not None:
        save_config(config_path, config)
    return require_string(config["google_oauth"], "access_token", "[google_oauth]")


def google_get(config: dict[str, Any], path: str, params: dict[str, Any]) -> dict[str, Any]:
    token = google_access_token(config)
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{GOOGLE_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    return urlopen_json(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )


def list_google_calendars_command(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config)
    try:
        config = load_config(config_path)
        calendars = list_google_calendars(config, config_path)
        for calendar in calendars:
            marker = " primary" if calendar.get("primary") else ""
            access_role = calendar.get("accessRole", "unknown")
            print(
                f"{calendar.get('id')}\t{calendar.get('summary', '')}\t{access_role}{marker}"
            )
        return 0
    except (ConfigError, FetchError, OSError, urllib.error.URLError) as exc:
        eprint(f"ERROR: {exc}")
        return 1


def list_google_calendars(
    config: dict[str, Any], config_path: pathlib.Path | None = None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"maxResults": 250}
        if page_token:
            params["pageToken"] = page_token
        token = google_access_token(config, config_path)
        query = urllib.parse.urlencode(params)
        response = urlopen_json(
            f"{GOOGLE_API_BASE}/users/me/calendarList?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        batch = response.get("items", [])
        if not isinstance(batch, list):
            raise FetchError("Google calendarList response had invalid items.")
        items.extend(item for item in batch if isinstance(item, dict))
        page_token = response.get("nextPageToken")
        if not isinstance(page_token, str):
            break
    return items


def extract_google_source(
    config: dict[str, Any],
    source: dict[str, Any],
    config_path: pathlib.Path | None = None,
) -> SourceResult:
    events_json = list_google_events(config, source, config_path)
    parent_uid_by_google_id = google_parent_uid_map(events_json)
    events = filter_events_for_source_options(
        [
            google_event_to_vevent(event, parent_uid_by_google_id)
            for event in events_json
        ],
        source,
    )
    return SourceResult(
        source_id=source["id"],
        name=source["name"],
        color=source["color"],
        events=events,
        timezones=[],
    )


def google_parent_uid_map(events: list[dict[str, Any]]) -> dict[str, str]:
    parent_uid_by_google_id: dict[str, str] = {}
    for event in events:
        google_id = event.get("id")
        if not isinstance(google_id, str) or not google_id:
            continue
        uid = event.get("iCalUID")
        parent_uid_by_google_id[google_id] = uid if isinstance(uid, str) and uid else google_id
    return parent_uid_by_google_id


def list_google_events(
    config: dict[str, Any],
    source: dict[str, Any],
    config_path: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    calendar_id = source["calendar_id"]
    encoded_calendar_id = urllib.parse.quote(calendar_id, safe="")
    events: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params = google_events_query_params(source)
        if page_token:
            params["pageToken"] = page_token
        token = google_access_token(config, config_path)
        query = urllib.parse.urlencode(params)
        response = urlopen_json(
            f"{GOOGLE_API_BASE}/calendars/{encoded_calendar_id}/events?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        batch = response.get("items", [])
        if not isinstance(batch, list):
            raise FetchError("Google events response had invalid items.")
        events.extend(event for event in batch if isinstance(event, dict))
        page_token = response.get("nextPageToken")
        if not isinstance(page_token, str):
            break
    return events


def google_events_query_params(source: dict[str, Any]) -> dict[str, Any]:
    max_results = int(source.get("google_max_results", 2500))
    if max_results < 1 or max_results > 2500:
        raise ConfigError(
            f"Source {source['id']} google_max_results must be between 1 and 2500."
        )
    params: dict[str, Any] = {
        "maxResults": max_results,
        "singleEvents": "false",
        "showDeleted": "false" if source.get("exclude_cancelled") else "true",
    }
    start, end = source_window(source)
    if start is not None:
        params["timeMin"] = google_bound_datetime(start)
    if end is not None:
        params["timeMax"] = google_bound_datetime(end + dt.timedelta(days=1))
    return params


def google_bound_datetime(value: dt.date) -> str:
    return dt.datetime(
        value.year,
        value.month,
        value.day,
        tzinfo=dt.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def google_event_to_vevent(
    event: dict[str, Any],
    parent_uid_by_google_id: dict[str, str] | None = None,
) -> IcsComponent:
    properties: list[str] = []
    recurring_event_id = event.get("recurringEventId")
    if (
        isinstance(recurring_event_id, str)
        and parent_uid_by_google_id
        and recurring_event_id in parent_uid_by_google_id
    ):
        uid = parent_uid_by_google_id[recurring_event_id]
    else:
        uid = event.get("iCalUID") or recurring_event_id or event.get("id")
    if not isinstance(uid, str) or not uid:
        raise FetchError("Google event is missing an id/iCalUID.")
    properties.append(f"UID:{ics_escape_text(uid)}")

    updated = event.get("updated") or event.get("created")
    if isinstance(updated, str):
        properties.append(f"DTSTAMP:{format_utc_datetime(parse_rfc3339_datetime(updated))}")
    else:
        properties.append(f"DTSTAMP:{format_utc_datetime(dt.datetime.now(dt.timezone.utc))}")

    text_mappings = [
        ("summary", "SUMMARY"),
        ("location", "LOCATION"),
    ]
    for google_key, ics_key in text_mappings:
        value = event.get(google_key)
        if isinstance(value, str) and value:
            properties.append(f"{ics_key}:{ics_escape_text(value)}")

    description = google_description(event)
    if description:
        properties.append(f"DESCRIPTION:{ics_escape_text(description)}")

    start = event.get("start")
    end = event.get("end")
    if isinstance(start, dict):
        properties.append(google_time_to_ics_property("DTSTART", start))
    if isinstance(end, dict):
        properties.append(google_time_to_ics_property("DTEND", end))

    original_start = event.get("originalStartTime")
    if isinstance(original_start, dict):
        properties.append(google_time_to_ics_property("RECURRENCE-ID", original_start))

    status = event.get("status")
    if isinstance(status, str) and status:
        properties.append(f"STATUS:{google_status_to_ics(status)}")

    transparency = event.get("transparency")
    if transparency == "transparent":
        properties.append("TRANSP:TRANSPARENT")
    elif transparency == "opaque":
        properties.append("TRANSP:OPAQUE")

    if isinstance(event.get("sequence"), int):
        properties.append(f"SEQUENCE:{event['sequence']}")

    for google_key, ics_key in (("created", "CREATED"), ("updated", "LAST-MODIFIED")):
        value = event.get(google_key)
        if isinstance(value, str):
            properties.append(f"{ics_key}:{format_utc_datetime(parse_rfc3339_datetime(value))}")

    html_link = event.get("htmlLink")
    if isinstance(html_link, str) and html_link:
        properties.append(f"URL:{escape_uri(html_link)}")

    event_type = event.get("eventType")
    if isinstance(event_type, str) and event_type:
        properties.append(f"X-GOOGLE-EVENTTYPE:{ics_escape_text(event_type)}")

    organizer = event.get("organizer")
    if isinstance(organizer, dict):
        organizer_line = google_person_to_ics("ORGANIZER", organizer)
        if organizer_line:
            properties.append(organizer_line)

    attendees = event.get("attendees")
    if isinstance(attendees, list):
        for attendee in attendees:
            if isinstance(attendee, dict):
                attendee_line = google_person_to_ics("ATTENDEE", attendee)
                if attendee_line:
                    properties.append(attendee_line)

    recurrence = event.get("recurrence")
    if isinstance(recurrence, list):
        for line in recurrence:
            if isinstance(line, str) and line:
                properties.append(line)

    children = google_reminders_to_valarms(event.get("reminders"))
    return IcsComponent("VEVENT", properties, children)


def google_description(event: dict[str, Any]) -> str:
    parts: list[str] = []
    description = event.get("description")
    if isinstance(description, str) and description:
        parts.append(description)
    conference_lines = google_conference_lines(event)
    if conference_lines:
        if parts:
            parts.append("")
        parts.extend(conference_lines)
    source = event.get("source")
    if isinstance(source, dict) and isinstance(source.get("url"), str):
        if parts:
            parts.append("")
        label = source.get("title") if isinstance(source.get("title"), str) else "Source"
        parts.append(f"{label}: {source['url']}")
    return "\n".join(parts)


def google_conference_lines(event: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    hangout_link = event.get("hangoutLink")
    if isinstance(hangout_link, str) and hangout_link:
        lines.append(f"Google Meet: {hangout_link}")
    conference = event.get("conferenceData")
    if not isinstance(conference, dict):
        return lines
    entry_points = conference.get("entryPoints")
    if not isinstance(entry_points, list):
        return lines
    for entry_point in entry_points:
        if not isinstance(entry_point, dict):
            continue
        uri = entry_point.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        label = entry_point.get("label") or entry_point.get("entryPointType") or "Conference"
        lines.append(f"{label}: {uri}")
    return lines


def google_time_to_ics_property(name: str, value: dict[str, Any]) -> str:
    date_value = value.get("date")
    if isinstance(date_value, str) and date_value:
        return f"{name};VALUE=DATE:{date_value.replace('-', '')}"
    date_time = value.get("dateTime")
    if not isinstance(date_time, str) or not date_time:
        raise FetchError(f"Google event time missing date/dateTime for {name}.")
    timezone_name = value.get("timeZone")
    parsed = parse_rfc3339_datetime(date_time)
    if isinstance(timezone_name, str) and timezone_name:
        local_value = parsed
        try:
            from zoneinfo import ZoneInfo

            local_value = parsed.astimezone(ZoneInfo(timezone_name))
        except Exception:
            local_value = parsed
        return f"{name};TZID={timezone_name}:{local_value.strftime('%Y%m%dT%H%M%S')}"
    return f"{name}:{format_utc_datetime(parsed)}"


def parse_rfc3339_datetime(value: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_utc_datetime(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def google_status_to_ics(status: str) -> str:
    mapping = {
        "confirmed": "CONFIRMED",
        "tentative": "TENTATIVE",
        "cancelled": "CANCELLED",
    }
    return mapping.get(status, status.upper())


def google_response_status_to_partstat(status: str) -> str:
    mapping = {
        "accepted": "ACCEPTED",
        "declined": "DECLINED",
        "tentative": "TENTATIVE",
        "needsAction": "NEEDS-ACTION",
    }
    return mapping.get(status, status.upper())


def google_person_to_ics(kind: str, person: dict[str, Any]) -> str | None:
    email = person.get("email")
    if not isinstance(email, str) or not email:
        return None
    params: list[str] = []
    display_name = person.get("displayName")
    if isinstance(display_name, str) and display_name:
        params.append(f"CN={quote_param(display_name)}")
    if kind == "ATTENDEE":
        response_status = person.get("responseStatus")
        if isinstance(response_status, str) and response_status:
            params.append(f"PARTSTAT={google_response_status_to_partstat(response_status)}")
        params.append(
            "ROLE=OPT-PARTICIPANT" if person.get("optional") else "ROLE=REQ-PARTICIPANT"
        )
        if person.get("resource"):
            params.append("CUTYPE=RESOURCE")
    param_text = "".join(f";{param}" for param in params)
    return f"{kind}{param_text}:mailto:{email}"


def google_reminders_to_valarms(reminders: Any) -> list[IcsComponent]:
    if not isinstance(reminders, dict):
        return []
    if reminders.get("useDefault"):
        return []
    overrides = reminders.get("overrides")
    if not isinstance(overrides, list):
        return []
    alarms: list[IcsComponent] = []
    for reminder in overrides:
        if not isinstance(reminder, dict):
            continue
        minutes = reminder.get("minutes")
        method = reminder.get("method")
        if not isinstance(minutes, int) or not isinstance(method, str):
            continue
        action = "EMAIL" if method == "email" else "DISPLAY"
        alarms.append(
            IcsComponent(
                "VALARM",
                [
                    f"TRIGGER:-PT{minutes}M",
                    f"ACTION:{action}",
                    "DESCRIPTION:Reminder",
                ],
                [],
            )
        )
    return alarms


def escape_uri(value: str) -> str:
    return value.replace("\r", "").replace("\n", "")


def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        return toml_quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if value is None:
        return '""'
    raise ConfigError(f"Cannot serialize value to TOML: {value!r}")


def save_config(path: pathlib.Path, config: dict[str, Any]) -> None:
    path.write_text(dump_config(config), encoding="utf-8")


def dump_config(config: dict[str, Any]) -> str:
    lines: list[str] = []
    scalar_items = {
        key: value
        for key, value in config.items()
        if not isinstance(value, (dict, list))
    }
    for key, value in scalar_items.items():
        lines.append(f"{key} = {toml_value(value)}")
    if scalar_items:
        lines.append("")

    for section_name in ("calendar", "google_oauth", "s3"):
        section = config.get(section_name)
        if isinstance(section, dict):
            lines.append(f"[{section_name}]")
            for key, value in section.items():
                if isinstance(value, dict):
                    continue
                lines.append(f"{key} = {toml_value(value)}")
            lines.append("")

    for array_name in ("calendars", "outputs"):
        entries = config.get(array_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lines.append(f"[[{array_name}]]")
            for key, value in entry.items():
                if isinstance(value, dict):
                    continue
                lines.append(f"{key} = {toml_value(value)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def load_aws_credentials(
    s3_config: dict[str, Any] | None = None,
) -> tuple[AwsCredentials, str | None]:
    s3_config = s3_config or {}
    access_key = s3_config.get("aws_access_key_id")
    secret_key = s3_config.get("aws_secret_access_key")
    session_token = s3_config.get("aws_session_token")
    region = s3_config.get("region")
    if any(value is not None for value in (access_key, secret_key, session_token)):
        if not isinstance(access_key, str) or not access_key:
            raise ConfigError("[s3].aws_access_key_id must be a non-empty string.")
        if not isinstance(secret_key, str) or not secret_key:
            raise ConfigError("[s3].aws_secret_access_key must be a non-empty string.")
        if session_token is not None and not isinstance(session_token, str):
            raise ConfigError("[s3].aws_session_token must be a string if present.")
        return AwsCredentials(access_key, secret_key, session_token), (
            region if isinstance(region, str) and region else None
        )

    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if access_key and secret_key:
        return AwsCredentials(access_key, secret_key, session_token), region

    profile = s3_config.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise ConfigError("[s3].profile must be a string if present.")
    profile_name = profile or os.environ.get("AWS_PROFILE") or "default"
    credentials_parser = configparser.ConfigParser()
    credentials_parser.read(os.path.expanduser("~/.aws/credentials"))
    if credentials_parser.has_section(profile_name):
        section = credentials_parser[profile_name]
        access_key = section.get("aws_access_key_id")
        secret_key = section.get("aws_secret_access_key")
        session_token = section.get("aws_session_token")

    config_parser = configparser.ConfigParser()
    config_parser.read(os.path.expanduser("~/.aws/config"))
    config_section = "default" if profile_name == "default" else f"profile {profile_name}"
    if config_parser.has_section(config_section):
        region = region or config_parser[config_section].get("region")

    if not access_key or not secret_key:
        raise ConfigError(
            "AWS credentials not found in environment or shared AWS credentials file."
        )
    return AwsCredentials(access_key, secret_key, session_token), region


def upload_outputs_to_s3(
    config: dict[str, Any], written: list[tuple[OutputSpec, pathlib.Path]]
) -> None:
    s3_config = config.get("s3") or {}
    if not isinstance(s3_config, dict):
        raise ConfigError("[s3] must be a table.")
    bucket = require_string(s3_config, "bucket", "[s3]")
    configured_region = s3_config.get("region")
    if configured_region is not None and not isinstance(configured_region, str):
        raise ConfigError("[s3].region must be a string if present.")
    credentials, env_region = load_aws_credentials(s3_config)
    region = configured_region or env_region
    if not isinstance(region, str) or not region:
        raise ConfigError("S3 region must be set in [s3].region or AWS config.")
    gzip_upload = s3_gzip_enabled(s3_config)
    content_encoding = "gzip" if gzip_upload else None

    for output, path in written:
        key = output.s3_key or s3_config.get("key")
        if not isinstance(key, str) or not key:
            raise ConfigError(f"Output {output.name!r} does not define an S3 key.")
        body = path.read_bytes()
        if gzip_upload:
            body = gzip.compress(body, mtime=0)
        upload_to_s3(
            bucket,
            key,
            region,
            credentials,
            body,
            content_encoding=content_encoding,
        )
        print(f"Uploaded s3://{bucket}/{key}")
        if content_encoding:
            print(f"Content-Encoding: {content_encoding}")
        print(f"HTTP URL: {s3_https_url(bucket, key, region)}")


def s3_https_url(bucket: str, key: str, region: str) -> str:
    escaped_key = "/".join(urllib.parse.quote(part, safe="") for part in key.split("/"))
    return f"https://{bucket}.s3.{region}.amazonaws.com/{escaped_key}"


def upload_to_s3(
    bucket: str,
    key: str,
    region: str,
    credentials: AwsCredentials,
    body: bytes,
    now: dt.datetime | None = None,
    content_encoding: str | None = None,
) -> int:
    request = build_s3_put_request(
        bucket,
        key,
        region,
        credentials,
        body,
        now,
        content_encoding=content_encoding,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise FetchError(f"S3 upload failed with HTTP {exc.code}: {body_text}") from exc


def build_s3_put_request(
    bucket: str,
    key: str,
    region: str,
    credentials: AwsCredentials,
    body: bytes,
    now: dt.datetime | None = None,
    content_encoding: str | None = None,
) -> urllib.request.Request:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    service = "s3"
    host = f"{bucket}.s3.{region}.amazonaws.com"
    canonical_uri = "/" + "/".join(
        urllib.parse.quote(part, safe="") for part in key.split("/")
    )
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "Content-Type": "text/calendar; charset=utf-8",
        "Host": host,
        "X-Amz-Acl": "public-read",
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amz_date,
    }
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    if credentials.session_token:
        headers["X-Amz-Security-Token"] = credentials.session_token

    signed_header_names = sorted(header.lower() for header in headers)
    canonical_headers = "".join(
        f"{name}:{headers[canonical_header_lookup(headers, name)].strip()}\n"
        for name in signed_header_names
    )
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join(
        [
            "PUT",
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = aws_v4_signing_key(credentials.secret_key, date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={credentials.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = s3_https_url(bucket, key, region)
    return urllib.request.Request(url, data=body, method="PUT", headers=headers)


def canonical_header_lookup(headers: dict[str, str], lowercase_name: str) -> str:
    for name in headers:
        if name.lower() == lowercase_name:
            return name
    raise KeyError(lowercase_name)


def aws_v4_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(
        ("AWS4" + secret_key).encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Combine ICS and Google Calendar sources into generated ICS feeds."
    )
    subparsers = parser.add_subparsers(dest="command")

    combine_parser = subparsers.add_parser("combine", help="Generate configured ICS feeds.")
    combine_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    combine_parser.add_argument(
        "--output",
        help="Output path for the default combined feed when no [[outputs]] are configured.",
    )
    combine_parser.add_argument(
        "--push-s3",
        action="store_true",
        help="Upload generated feeds to S3 after writing them.",
    )
    combine_parser.set_defaults(func=combine_command)

    upload_parser = subparsers.add_parser(
        "upload", help="Upload existing configured ICS feeds to S3."
    )
    upload_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    upload_parser.add_argument(
        "--output",
        help="Output path for the default combined feed when no [[outputs]] are configured.",
    )
    upload_parser.set_defaults(func=upload_command)

    auth_parser = subparsers.add_parser(
        "auth-google", help="Authorize Google Calendar and save OAuth tokens."
    )
    auth_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    auth_parser.add_argument("--port", type=int, default=0)
    auth_parser.add_argument("--timeout-seconds", type=int, default=180)
    auth_parser.add_argument("--no-browser", action="store_true")
    auth_parser.set_defaults(func=auth_google_command)

    list_parser = subparsers.add_parser(
        "list-google-calendars", help="List calendars visible to saved Google OAuth token."
    )
    list_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    list_parser.set_defaults(func=list_google_calendars_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    commands = {
        "combine",
        "upload",
        "auth-google",
        "list-google-calendars",
        "-h",
        "--help",
    }
    if not argv or argv[0] not in commands:
        argv = ["combine", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
