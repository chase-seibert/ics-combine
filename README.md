# ICS Combine

`ics-combine` is a stdlib-only Python 3 command line utility for combining multiple calendar sources into generated ICS feeds. Sources can be public or private ICS URLs, or Google Calendars fetched through OAuth when an ICS feed cannot be made public.

It can write local `.ics` files and optionally upload them to S3.

## Requirements

- Python 3.11 or newer
- `make`
- AWS credentials only if using S3 upload
- A Google OAuth desktop app client only if using Google Calendar sources

No Python packages need to be installed.

## Quick Start

Create your private config:

```sh
make init-config
```

Edit `calendars.toml` with your calendar sources, colors, output files, and S3 destination. This file is intentionally ignored by git because it may contain private feed URLs, OAuth tokens, and secrets.

Generate feeds locally:

```sh
make combine
```

Generate feeds and upload them to S3:

```sh
make combine-push
```

Upload the already-generated local feed files to S3 without recombining:

```sh
make push
```

Run tests:

```sh
make test
```

## Config

Use `calendars.example.toml` as the starting point.

Optionally set a display timezone for the generated feed:

```toml
[calendar]
timezone = "America/Los_Angeles"
```

When set, generated feeds include `X-WR-TIMEZONE`. Event-level `TZID` and `VTIMEZONE` data still control actual event times; this is a display hint for clients that understand it.

Each calendar source needs a stable `id`, a human-readable `name`, and an event `color`.

ICS URL source:

```toml
[[calendars]]
id = "team-holidays"
type = "ics"
name = "Team Holidays"
url = "webcal://example.com/holidays.ics"
color = "tomato"
```

`http://`, `https://`, `webcal://`, and `webcals://` feed URLs are supported. `webcal://` URLs are fetched as HTTPS first, with HTTP as a fallback; `webcals://` URLs are fetched as HTTPS.

Google Calendar source:

```toml
[[calendars]]
id = "alice-default"
type = "google"
name = "Alice Default"
calendar_id = "primary"
color = "cornflowerblue"
include_past_days = 90
include_future_days = 730
exclude_cancelled = true
```

The optional `include_past_days` and `include_future_days` fields keep large calendars small enough for calendar clients to import or subscribe to comfortably. Old one-off events outside the window are removed. Recurring events are kept when they have no `UNTIL` date or their `UNTIL` date is inside the window.

For Google Calendar sources, the same window is also sent to the Google Calendar API as `timeMin` and `timeMax`, which avoids downloading the full calendar history. Set `exclude_cancelled = true` on a source to request `showDeleted=false` from Google and remove events whose ICS status is `CANCELLED` locally. This is especially useful where cancelled meetings may otherwise appear as recurrence exceptions or deleted events.

When `[[outputs]]` entries are configured, only those feeds are generated. Each output lists the source IDs that should go into it.

```toml
[[outputs]]
name = "Alice Combined Feed"
file = "dist/alice.ics"
s3_key = "calendars/alice.ics"
include_source_ids = ["bob-default", "team-holidays"]
```

If no `[[outputs]]` entries are configured, the tool writes one combined feed containing all successful sources. The default path is `combined.ics`, or whatever is passed with `--output`.

## Google OAuth

For corporate calendars that cannot expose public ICS feeds, create a Google OAuth desktop app client and put the client credentials in `calendars.toml`:

```toml
[google_oauth]
client_id = "replace-me.apps.googleusercontent.com"
client_secret = "replace-me"
scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
```

Authorize and save tokens:

```sh
make auth-google
```

List calendars visible to the authorized account:

```sh
make list-google-calendars
```

Use the calendar ID you want in a `type = "google"` calendar source.

## S3 Upload

Configure the destination bucket and region:

```toml
[s3]
bucket = "my-calendar-feed-bucket"
region = "us-west-2"
profile = "default"
# Optional: upload gzipped bytes and serve them as the same ICS content.
gzip = true
# Optional, if you want this gitignored TOML file to carry AWS credentials.
aws_access_key_id = "replace-me"
aws_secret_access_key = "replace-me"
aws_session_token = "replace-me"
```

Each output should provide its own `s3_key`. If no explicit outputs are configured, `[s3].key` is used for the single combined feed.

AWS credentials are read from `[s3]` first when `aws_access_key_id` and `aws_secret_access_key` are set, then from environment variables, then from the shared AWS credentials/config files. The upload is implemented directly in Python with AWS Signature Version 4.

Set `[s3].gzip = true` to gzip the object before upload and store it with `Content-Encoding: gzip`. The S3 key can remain `*.ics`; HTTP clients that honor content encodings will decompress the stream and see the same calendar contents.

Uploaded objects are written with the S3 canned ACL `public-read`, so the logged HTTP URL can be used by calendar clients. The bucket must allow ACLs and the AWS credentials need permission to set the object ACL.

## Calendar Behavior

- Original ICS event UIDs are preserved.
- Google events use `iCalUID` when available, falling back to the Google event ID.
- Google recurring events preserve recurrence metadata instead of expanding into a fixed date window.
- Generated feeds include `X-WR-TIMEZONE` when `[calendar].timezone` is configured.
- Sources can optionally filter old/far-future events with `include_past_days` and `include_future_days`.
- Sources can optionally remove cancelled events with `exclude_cancelled = true`.
- Outputs can optionally remove top-level event descriptions with `skip_description = true`.
- Outputs can optionally remove top-level event attendees with `skip_attendees = true`.
- Event fields such as title, start/end, recurrence metadata, attendees, attendee response status, location, description, organizer, alarms, and vendor fields are preserved where available.
- The source calendar name is appended to each event description unless `skip_description = true`.
- Each event gets a standards-compliant RFC 7986 `COLOR` property from its parent calendar source.
- If one source fails, it is skipped with a warning. If an output has no successful included sources, the command exits nonzero.

## Direct CLI Usage

The Makefile wraps these commands:

```sh
python3 combine_ics.py combine --config calendars.toml
python3 combine_ics.py combine --config calendars.toml --push-s3
python3 combine_ics.py upload --config calendars.toml
python3 combine_ics.py auth-google --config calendars.toml
python3 combine_ics.py list-google-calendars --config calendars.toml
```
