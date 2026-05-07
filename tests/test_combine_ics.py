import contextlib
import datetime as dt
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import combine_ics


class IcsParsingTests(unittest.TestCase):
    def test_fold_and_unfold_round_trip_long_line(self):
        line = "DESCRIPTION:" + ("a" * 120)
        folded = combine_ics.fold_ics_line(line)
        self.assertGreater(len(folded), 1)
        unfolded = combine_ics.unfold_ics_lines("\r\n".join(folded))
        self.assertEqual([line], unfolded)

    def test_transform_event_preserves_nested_alarm_and_appends_source(self):
        root = combine_ics.parse_ics(
            "\r\n".join(
                [
                    "BEGIN:VCALENDAR",
                    "VERSION:2.0",
                    "BEGIN:VEVENT",
                    "UID:event-1",
                    "SUMMARY:Demo",
                    "DESCRIPTION:Existing description",
                    "COLOR:red",
                    "BEGIN:VALARM",
                    "TRIGGER:-PT10M",
                    "ACTION:DISPLAY",
                    "DESCRIPTION:Reminder",
                    "END:VALARM",
                    "END:VEVENT",
                    "END:VCALENDAR",
                    "",
                ]
            )
        )
        event = root.children[0]
        transformed = combine_ics.transform_event(event, "Work Calendar", "turquoise")
        rendered = "\n".join(combine_ics.component_to_unfolded_lines(transformed))

        self.assertIn("COLOR:turquoise", rendered)
        self.assertNotIn("COLOR:red", rendered)
        self.assertIn("Existing description\\n\\nSource calendar: Work Calendar", rendered)
        self.assertIn("BEGIN:VALARM", rendered)
        self.assertIn("TRIGGER:-PT10M", rendered)


class FetchTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, body, content_type="text/calendar; charset=utf-8"):
            self.body = body
            self.headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.body

    def test_webcal_url_tries_https_first(self):
        seen_urls = []

        def fake_urlopen(request, timeout=30):
            seen_urls.append(request.full_url)
            return self.FakeResponse(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

        with mock.patch.object(combine_ics.urllib.request, "urlopen", side_effect=fake_urlopen):
            text = combine_ics.read_text_response("webcal://example.com/feed.ics")

        self.assertEqual("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", text)
        self.assertEqual(["https://example.com/feed.ics"], seen_urls)

    def test_webcal_url_falls_back_to_http(self):
        seen_urls = []

        def fake_urlopen(request, timeout=30):
            seen_urls.append(request.full_url)
            if request.full_url.startswith("https://"):
                raise combine_ics.urllib.error.URLError("no https")
            return self.FakeResponse(b"ok")

        with mock.patch.object(combine_ics.urllib.request, "urlopen", side_effect=fake_urlopen):
            text = combine_ics.read_text_response("webcal://example.com/feed.ics")

        self.assertEqual("ok", text)
        self.assertEqual(
            ["https://example.com/feed.ics", "http://example.com/feed.ics"],
            seen_urls,
        )

    def test_webcals_url_maps_to_https(self):
        self.assertEqual(
            ["https://example.com/feed.ics"],
            combine_ics.calendar_url_candidates("webcals://example.com/feed.ics"),
        )


class GoogleConversionTests(unittest.TestCase):
    def test_google_event_to_vevent_preserves_expected_fields(self):
        event = {
            "id": "google-id",
            "iCalUID": "ical-uid@example.com",
            "summary": "Planning",
            "description": "Agenda",
            "location": "Room 1",
            "status": "confirmed",
            "created": "2026-05-01T12:00:00Z",
            "updated": "2026-05-02T12:00:00Z",
            "sequence": 7,
            "htmlLink": "https://calendar.google.com/event?eid=abc",
            "eventType": "default",
            "start": {
                "dateTime": "2026-05-07T09:00:00-07:00",
                "timeZone": "America/Los_Angeles",
            },
            "end": {
                "dateTime": "2026-05-07T10:00:00-07:00",
                "timeZone": "America/Los_Angeles",
            },
            "originalStartTime": {
                "dateTime": "2026-05-07T09:00:00-07:00",
                "timeZone": "America/Los_Angeles",
            },
            "recurrence": [
                "RRULE:FREQ=WEEKLY;BYDAY=TH",
                "EXDATE;TZID=America/Los_Angeles:20260514T090000",
            ],
            "organizer": {"email": "lead@example.com", "displayName": "Team Lead"},
            "attendees": [
                {
                    "email": "pat@example.com",
                    "displayName": "Pat Person",
                    "responseStatus": "accepted",
                },
                {
                    "email": "room@example.com",
                    "displayName": "Board Room",
                    "resource": True,
                    "responseStatus": "needsAction",
                },
            ],
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc"}
                ]
            },
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 10}],
            },
        }

        vevent = combine_ics.google_event_to_vevent(event)
        rendered = "\n".join(combine_ics.component_to_unfolded_lines(vevent))

        self.assertIn("UID:ical-uid@example.com", rendered)
        self.assertIn("SUMMARY:Planning", rendered)
        self.assertIn("LOCATION:Room 1", rendered)
        self.assertIn("DTSTART;TZID=America/Los_Angeles:20260507T090000", rendered)
        self.assertIn("DTEND;TZID=America/Los_Angeles:20260507T100000", rendered)
        self.assertIn("RECURRENCE-ID;TZID=America/Los_Angeles:20260507T090000", rendered)
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=TH", rendered)
        self.assertIn("STATUS:CONFIRMED", rendered)
        self.assertIn("SEQUENCE:7", rendered)
        self.assertIn("ORGANIZER;CN=\"Team Lead\":mailto:lead@example.com", rendered)
        self.assertIn(
            "ATTENDEE;CN=\"Pat Person\";PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:pat@example.com",
            rendered,
        )
        self.assertIn("CUTYPE=RESOURCE", rendered)
        self.assertIn("https://meet.google.com/abc", rendered)
        self.assertIn("BEGIN:VALARM", rendered)

    def test_google_all_day_event_uses_date_values(self):
        vevent = combine_ics.google_event_to_vevent(
            {
                "id": "all-day",
                "summary": "OOO",
                "updated": "2026-05-02T12:00:00Z",
                "start": {"date": "2026-05-07"},
                "end": {"date": "2026-05-08"},
            }
        )
        rendered = "\n".join(combine_ics.component_to_unfolded_lines(vevent))
        self.assertIn("DTSTART;VALUE=DATE:20260507", rendered)
        self.assertIn("DTEND;VALUE=DATE:20260508", rendered)

    def test_google_exception_uses_parent_uid_when_available(self):
        vevent = combine_ics.google_event_to_vevent(
            {
                "id": "exception-google-id",
                "recurringEventId": "series-google-id",
                "status": "cancelled",
                "updated": "2026-05-02T12:00:00Z",
                "originalStartTime": {
                    "dateTime": "2026-05-14T09:00:00-07:00",
                    "timeZone": "America/Los_Angeles",
                },
            },
            {"series-google-id": "series-ical-uid@example.com"},
        )
        rendered = "\n".join(combine_ics.component_to_unfolded_lines(vevent))
        self.assertIn("UID:series-ical-uid@example.com", rendered)
        self.assertIn("STATUS:CANCELLED", rendered)
        self.assertIn("RECURRENCE-ID;TZID=America/Los_Angeles:20260514T090000", rendered)


class OutputSelectionTests(unittest.TestCase):
    def make_source(self, source_id, name):
        event = combine_ics.IcsComponent(
            "VEVENT",
            [
                f"UID:{source_id}-event",
                f"SUMMARY:{name}",
            ],
            [],
        )
        return combine_ics.SourceResult(source_id, name, "turquoise", [event], [])

    def test_explicit_output_excludes_configured_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = pathlib.Path(tmpdir) / "bob.ics"
            config = {
                "calendars": [],
                "outputs": [
                    {
                        "name": "Bob Feed",
                        "file": str(output_file),
                        "s3_key": "feeds/bob.ics",
                        "exclude_source_id": "alice",
                    }
                ],
            }
            written = combine_ics.write_outputs(
                config,
                [
                    self.make_source("alice", "Alice"),
                    self.make_source("bob", "Bob"),
                ],
            )
            self.assertEqual(output_file, written[0][1])
            text = output_file.read_text(encoding="utf-8")
            self.assertNotIn("UID:alice-event", text)
            self.assertIn("UID:bob-event", text)
            self.assertIn("Source calendar: Bob", text)

    def test_existing_output_files_uses_files_without_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = pathlib.Path(tmpdir) / "combined.ics"
            output_file.write_text("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", encoding="utf-8")
            config = {"calendars": [], "s3": {"key": "combined.ics"}}

            existing = combine_ics.existing_output_files(config, str(output_file))

            self.assertEqual("Combined Calendar", existing[0][0].name)
            self.assertEqual(output_file, existing[0][1])

    def test_existing_output_files_errors_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = pathlib.Path(tmpdir) / "missing.ics"

            with self.assertRaises(combine_ics.FetchError):
                combine_ics.existing_output_files(
                    {"calendars": [], "s3": {"key": "combined.ics"}},
                    str(missing),
                )

    def test_duplicate_timezones_are_deduped_by_tzid(self):
        timezone_a = combine_ics.IcsComponent(
            "VTIMEZONE",
            ["TZID:America/Los_Angeles"],
            [combine_ics.IcsComponent("STANDARD", ["TZOFFSETFROM:-0700"], [])],
        )
        timezone_b = combine_ics.IcsComponent(
            "VTIMEZONE",
            ["TZID:America/Los_Angeles"],
            [combine_ics.IcsComponent("STANDARD", ["TZOFFSETFROM:-0800"], [])],
        )
        source_a = self.make_source("a", "A")
        source_b = self.make_source("b", "B")
        source_a.timezones = [timezone_a]
        source_b.timezones = [timezone_b]

        calendar = combine_ics.build_output_calendar(
            combine_ics.OutputSpec("Combined", "combined.ics", None, None),
            [source_a, source_b],
        )

        timezones = [child for child in calendar.children if child.name == "VTIMEZONE"]
        self.assertEqual(1, len(timezones))
        self.assertEqual("America/Los_Angeles", combine_ics.component_property_value(timezones[0], "TZID"))

    def test_build_output_calendar_adds_configured_timezone_hint(self):
        calendar = combine_ics.build_output_calendar(
            combine_ics.OutputSpec("Combined", "combined.ics", None, None),
            [self.make_source("a", "A")],
            "America/Los_Angeles",
        )

        self.assertIn("X-WR-TIMEZONE:America/Los_Angeles", calendar.properties)

    def test_calendar_timezone_validates_iana_name(self):
        self.assertEqual(
            "America/Los_Angeles",
            combine_ics.calendar_timezone(
                {"calendar": {"timezone": "America/Los_Angeles"}}
            ),
        )
        with self.assertRaises(combine_ics.ConfigError):
            combine_ics.calendar_timezone({"calendar": {"timezone": "Not/AZone"}})

    def test_dump_config_preserves_calendar_section(self):
        text = combine_ics.dump_config(
            {
                "calendar": {"timezone": "America/Los_Angeles"},
                "calendars": [],
            }
        )

        self.assertIn("[calendar]", text)
        self.assertIn('timezone = "America/Los_Angeles"', text)

    def test_source_window_filters_old_single_events_but_keeps_active_recurrence(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        old_date = (today - dt.timedelta(days=800)).strftime("%Y%m%d")
        recent_date = (today - dt.timedelta(days=10)).strftime("%Y%m%d")
        ended_until = (today - dt.timedelta(days=400)).strftime("%Y%m%d")
        active_until = (today + dt.timedelta(days=400)).strftime("%Y%m%d")
        events = [
            combine_ics.IcsComponent("VEVENT", ["UID:old", f"DTSTART;VALUE=DATE:{old_date}"], []),
            combine_ics.IcsComponent("VEVENT", ["UID:recent", f"DTSTART;VALUE=DATE:{recent_date}"], []),
            combine_ics.IcsComponent(
                "VEVENT",
                ["UID:ended", f"DTSTART;VALUE=DATE:{old_date}", f"RRULE:FREQ=DAILY;UNTIL={ended_until}"],
                [],
            ),
            combine_ics.IcsComponent(
                "VEVENT",
                ["UID:active", f"DTSTART;VALUE=DATE:{old_date}", f"RRULE:FREQ=DAILY;UNTIL={active_until}"],
                [],
            ),
        ]

        kept = combine_ics.filter_events_for_source_options(
            events,
            {"id": "source", "include_past_days": 90, "include_future_days": 730},
        )

        kept_uids = {
            combine_ics.component_property_value(event, "UID")
            for event in kept
        }
        self.assertEqual({"recent", "active"}, kept_uids)

    def test_source_option_excludes_cancelled_events(self):
        events = [
            combine_ics.IcsComponent("VEVENT", ["UID:keep", "STATUS:CONFIRMED"], []),
            combine_ics.IcsComponent("VEVENT", ["UID:drop", "STATUS:CANCELLED"], []),
        ]

        kept = combine_ics.filter_events_for_source_options(
            events,
            {"id": "source", "exclude_cancelled": True},
        )

        self.assertEqual(
            ["keep"],
            [combine_ics.component_property_value(event, "UID") for event in kept],
        )

    def test_google_events_query_params_uses_window_and_deleted_filter(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        params = combine_ics.google_events_query_params(
            {
                "id": "google",
                "calendar_id": "primary",
                "include_past_days": 90,
                "include_future_days": 730,
                "exclude_cancelled": True,
            }
        )

        self.assertEqual(2500, params["maxResults"])
        self.assertEqual("false", params["singleEvents"])
        self.assertEqual("false", params["showDeleted"])
        self.assertEqual(
            combine_ics.google_bound_datetime(today - dt.timedelta(days=90)),
            params["timeMin"],
        )
        self.assertEqual(
            combine_ics.google_bound_datetime(today + dt.timedelta(days=731)),
            params["timeMax"],
        )

    def test_list_google_events_sends_query_params_and_pages(self):
        source = {
            "id": "google",
            "calendar_id": "primary",
            "include_past_days": 1,
            "include_future_days": 1,
            "exclude_cancelled": True,
        }
        seen_urls = []

        def fake_urlopen_json(url, method="GET", data=None, headers=None, timeout=30):
            seen_urls.append(url)
            if len(seen_urls) == 1:
                return {"items": [{"id": "one"}], "nextPageToken": "next"}
            return {"items": [{"id": "two"}]}

        with mock.patch.object(combine_ics, "google_access_token", return_value="token"):
            with mock.patch.object(combine_ics, "urlopen_json", side_effect=fake_urlopen_json):
                events = combine_ics.list_google_events({}, source)

        self.assertEqual([{"id": "one"}, {"id": "two"}], events)
        first_query = dict(
            pair.split("=", 1)
            for pair in seen_urls[0].split("?", 1)[1].split("&")
        )
        second_query = dict(
            pair.split("=", 1)
            for pair in seen_urls[1].split("?", 1)[1].split("&")
        )
        self.assertEqual("false", first_query["showDeleted"])
        self.assertIn("timeMin", first_query)
        self.assertIn("timeMax", first_query)
        self.assertEqual("next", second_query["pageToken"])

    def test_validate_config_rejects_missing_excluded_source(self):
        with self.assertRaises(combine_ics.ConfigError):
            combine_ics.validate_config(
                {
                    "calendars": [
                        {
                            "id": "alice",
                            "type": "ics",
                            "name": "Alice",
                            "url": "https://example.com/a.ics",
                            "color": "red",
                        }
                    ],
                    "outputs": [
                        {
                            "name": "Bad",
                            "file": "bad.ics",
                            "exclude_source_id": "missing",
                        }
                    ],
                }
            )

    def test_fetch_all_sources_logs_input_progress_and_item_count(self):
        config = {
            "calendars": [
                {
                    "id": "alice",
                    "type": "ics",
                    "name": "Alice",
                    "url": "https://example.com/a.ics",
                    "color": "red",
                }
            ]
        }
        result = self.make_source("alice", "Alice")
        output = io.StringIO()

        with mock.patch.object(combine_ics, "extract_ics_source", return_value=result):
            with contextlib.redirect_stdout(output):
                sources, warnings = combine_ics.fetch_all_sources(config)

        self.assertEqual([result], sources)
        self.assertEqual([], warnings)
        text = output.getvalue()
        self.assertIn("Processing input 1/1: alice (Alice, ics)...", text)
        self.assertIn("Finished input 1/1: alice (Alice): 1 calendar item", text)


class AuthAndS3Tests(unittest.TestCase):
    def test_google_refresh_updates_config_token(self):
        config = {
            "google_oauth": {
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh",
                "access_token": "old",
                "token_expiry": "2020-01-01T00:00:00Z",
                "scopes": [combine_ics.GOOGLE_CALENDAR_READONLY_SCOPE],
            }
        }
        calls = []

        def fake_urlopen_json(url, method="GET", data=None, headers=None, timeout=30):
            calls.append((url, method, data))
            return {
                "access_token": "new-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }

        with mock.patch.object(combine_ics, "urlopen_json", side_effect=fake_urlopen_json):
            token = combine_ics.google_access_token(config)

        self.assertEqual("new-token", token)
        self.assertEqual("new-token", config["google_oauth"]["access_token"])
        self.assertEqual(combine_ics.GOOGLE_TOKEN_URL, calls[0][0])
        self.assertEqual("POST", calls[0][1])
        self.assertEqual("refresh_token", calls[0][2]["grant_type"])

    def test_s3_request_signing_is_deterministic_shape(self):
        request = combine_ics.build_s3_put_request(
            bucket="calendar-bucket",
            key="feeds/alice.ics",
            region="us-west-2",
            credentials=combine_ics.AwsCredentials(
                "AKIDEXAMPLE",
                "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                "session-token",
            ),
            body=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            now=dt.datetime(2026, 5, 7, 12, 0, 0, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(
            "https://calendar-bucket.s3.us-west-2.amazonaws.com/feeds/alice.ics",
            request.full_url,
        )
        self.assertEqual("PUT", request.get_method())
        self.assertEqual("public-read", request.get_header("X-amz-acl"))
        self.assertEqual("20260507T120000Z", request.get_header("X-amz-date"))
        self.assertEqual("session-token", request.get_header("X-amz-security-token"))
        authorization = request.get_header("Authorization")
        self.assertTrue(authorization.startswith("AWS4-HMAC-SHA256 "))
        self.assertIn("Credential=AKIDEXAMPLE/20260507/us-west-2/s3/aws4_request", authorization)
        self.assertIn(
            "SignedHeaders=content-type;host;x-amz-acl;x-amz-content-sha256;x-amz-date;x-amz-security-token",
            authorization,
        )

    def test_s3_https_url_escapes_key_segments(self):
        self.assertEqual(
            "https://calendar-bucket.s3.us-west-2.amazonaws.com/feeds/alice%20calendar.ics",
            combine_ics.s3_https_url(
                "calendar-bucket",
                "feeds/alice calendar.ics",
                "us-west-2",
            ),
        )

    def test_upload_outputs_logs_http_url(self):
        output_path = pathlib.Path("generated.ics")
        config = {
            "s3": {
                "bucket": "calendar-bucket",
                "region": "us-west-2",
                "aws_access_key_id": "access",
                "aws_secret_access_key": "secret",
            }
        }
        output = combine_ics.OutputSpec(
            "Generated",
            str(output_path),
            "feeds/generated.ics",
            None,
        )
        stdout = io.StringIO()

        with mock.patch.object(pathlib.Path, "read_bytes", return_value=b"ics"):
            with mock.patch.object(combine_ics, "upload_to_s3", return_value=200):
                with contextlib.redirect_stdout(stdout):
                    combine_ics.upload_outputs_to_s3(config, [(output, output_path)])

        text = stdout.getvalue()
        self.assertIn("Uploaded s3://calendar-bucket/feeds/generated.ics", text)
        self.assertIn(
            "HTTP URL: https://calendar-bucket.s3.us-west-2.amazonaws.com/feeds/generated.ics",
            text,
        )

    def test_s3_config_credentials_take_precedence(self):
        with mock.patch.dict(
            combine_ics.os.environ,
            {
                "AWS_ACCESS_KEY_ID": "env-access",
                "AWS_SECRET_ACCESS_KEY": "env-secret",
                "AWS_REGION": "us-east-1",
            },
            clear=True,
        ):
            credentials, region = combine_ics.load_aws_credentials(
                {
                    "aws_access_key_id": "toml-access",
                    "aws_secret_access_key": "toml-secret",
                    "aws_session_token": "toml-session",
                    "region": "us-west-1",
                }
            )

        self.assertEqual("toml-access", credentials.access_key)
        self.assertEqual("toml-secret", credentials.secret_key)
        self.assertEqual("toml-session", credentials.session_token)
        self.assertEqual("us-west-1", region)

    def test_s3_config_credentials_require_secret_pair(self):
        with self.assertRaises(combine_ics.ConfigError):
            combine_ics.load_aws_credentials({"aws_access_key_id": "only-access"})


if __name__ == "__main__":
    unittest.main()
