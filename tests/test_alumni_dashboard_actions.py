import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from csrf_test_helpers import csrf_token_from_html
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app.main import app
from app.seed import seed

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
PASSWORD = "alumni-dev"


@pytest.fixture
def client():
    seed(reset=True)
    return TestClient(app)


def _login(client: TestClient) -> TestClient:
    csrf_token = csrf_token_from_html(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "username": ALUMNI_EMAIL,
            "password": PASSWORD,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _sample_listing(listing_id: int, **overrides):
    listing = {
        "id": listing_id,
        "author_id": 2,
        "kind": "job",
        "title": f"Sample Listing {listing_id}",
        "body": "Sample body.",
        "status": "pending",
        "created_at": "2099-03-01T12:00:00+00:00",
    }
    listing.update(overrides)
    return listing


def _action_buttons(html):
    class ButtonParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buttons = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button":
                key = attrs.get("data-edit-id") or attrs.get("data-revise-id")
                if key:
                    self.buttons[key] = json.loads(attrs["data-listing"])

    parser = ButtonParser()
    parser.feed(html)
    return parser.buttons


def test_edit_button_only_for_pending_and_rejected(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(1, status="pending"),
            _sample_listing(2, status="rejected"),
            _sample_listing(3, status="published"),
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert 'data-edit-id="1"' in page.text
    assert 'data-edit-id="2"' in page.text
    assert 'data-edit-id="3"' not in page.text
    assert 'data-delete-id="1"' in page.text
    assert 'data-delete-id="2"' in page.text
    assert 'data-delete-id="3"' in page.text


def test_edit_button_prepopulates_existing_listing_data(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(
                4,
                kind="event",
                status="rejected",
                title="Rejected Event",
                body="Event body text.",
                location='Hall "A"',
                event_date="2099-12-01",
                event_time="13:45:30",
                category="Workshop",
                registration_information="Register online",
            ),
            _sample_listing(
                8,
                status="published",
                company_name="UWI Labs",
                location="Mona",
                faculty="Science",
                department="Computing",
                application_instructions="Apply online",
                application_deadline="2099-11-30",
                category="Technology",
            ),
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert 'data-edit-id="4"' in page.text
    assert 'data-revise-id="8"' in page.text
    buttons = _action_buttons(page.text)
    for field, value in {
        "kind": "event",
        "title": "Rejected Event",
        "body": "Event body text.",
        "location": 'Hall "A"',
        "event_date": "2099-12-01",
        "event_time": "13:45:30",
        "category": "Workshop",
        "registration_information": "Register online",
    }.items():
        assert buttons["4"][field] == value
    for field, value in {
        "company_name": "UWI Labs",
        "faculty": "Science",
        "department": "Computing",
        "application_instructions": "Apply online",
        "application_deadline": "2099-11-30",
        "category": "Technology",
    }.items():
        assert buttons["8"][field] == value


def test_listing_content_is_escaped_and_action_data_round_trips(client, monkeypatch):
    payload = '</script><img src=x onerror=alert(1)> & "quotes" \u2028\n'
    details = {
        "title": payload,
        "body": payload,
        "location": payload,
        "company_name": payload,
        "faculty": payload,
        "department": payload,
        "application_instructions": payload,
        "category": payload,
        "registration_information": payload,
    }
    job = _sample_listing(
        4, status="rejected", rejection_reason=payload,
        application_deadline='2099-12-01" onmouseover="alert(1)', **details
    )
    event = _sample_listing(
        8, kind="event", status="published",
        event_date='2099-12-01" onmouseover="alert(1)',
        event_time="13:<svg onload=alert(1)>", **details
    )
    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", lambda author_id: [job, event]
    )
    page = _login(client).get("/app/dashboard")

    assert "<img src=x onerror=alert(1)>" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert 'datetime="2099-12-01&#34; onmouseover=&#34;alert(1)' in page.text
    assert "&lt;s" in page.text
    buttons = _action_buttons(page.text)
    for key in ("4", "8"):
        for field, value in details.items():
            assert buttons[key][field] == value
    assert buttons["4"]["application_deadline"] == job["application_deadline"]
    assert buttons["8"]["event_date"] == event["event_date"]
    assert buttons["8"]["event_time"] == event["event_time"]


def test_published_listing_views_escape_listing_content():
    templates = Path(__file__).resolve().parents[1] / "app" / "templates"
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "authenticated-base.html": "{% block subpage_content %}{% endblock %}",
                "base.html": "{% block content %}{% endblock %}",
            }),
            FileSystemLoader(templates),
        ]),
        autoescape=False,
    )
    env.globals["url_for"] = lambda *args, **kwargs: "/"
    payload = '<img src=x onerror=alert(1)> & "quotes"'
    details = dict.fromkeys(
        (
            "title", "body", "company_name", "location", "faculty", "department",
            "application_instructions", "category", "registration_information",
        ),
        payload,
    )
    job = _sample_listing(1, application_deadline="2099-12-01", **details)
    event = _sample_listing(
        2, kind="event", event_date="2099-12-01", event_time="13:45", **details
    )
    board = env.get_template("app.html").render(
        listings=[job, event], q=payload, kind="", user={"role": "alumni"}
    )
    landing = env.get_template("landing.html").render(
        featured_jobs=[job], featured_events=[event], user_logged_in=False
    )
    for html in (board, landing):
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert 'value="&lt;img src=x onerror=alert(1)&gt; &amp; &#34;quotes&#34;"' in board
    assert "1 December 2099 at 1:45 p.m." in board

    short_dates = [
        _sample_listing(3, application_deadline="<b>", **details),
        _sample_listing(4, kind="event", event_date="<b>", event_time="<b>", **details),
    ]
    board = env.get_template("app.html").render(
        listings=short_dates, q="", kind="", user={"role": "alumni"}
    )
    assert "<b>" not in board
    assert "&lt;b&gt;" in board
    assert "&amp;lt;b&amp;gt;" not in board


def test_edit_form_contains_job_and_event_details(client):
    page = _login(client).get("/app/dashboard")

    for field in (
        "location",
        "category",
        "company_name",
        "faculty",
        "department",
        "application_instructions",
        "application_deadline",
        "event_date",
        "event_time",
        "registration_information",
    ):
        assert f'name="{field}"' in page.text
    assert 'id="editJobFields"' in page.text
    assert 'id="editEventFields"' in page.text


def test_deletion_requested_badge_replaces_action_buttons(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(5, status="published", deletion_requested=True)
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert "Deletion requested — awaiting admin review" in page.text
    assert 'data-delete-id="5"' not in page.text
    assert 'data-edit-id="5"' not in page.text


def test_dashboard_contains_agreed_api_contract_and_feedback(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [_sample_listing(6, status="rejected")]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert '/api/boards/listings/' in page.text
    assert "/deletion-request" in page.text
    assert "method: isRevision ? 'POST' : 'PUT'" in page.text
    assert "method: 'POST'" in page.text
    assert "Title and description are required." in page.text
    assert "Listing updated and sent for admin review." in page.text
    assert "Revision request sent. The published listing stays unchanged" in page.text
    assert "The listing stays visible until an admin approves it." in page.text
    assert "The listing service may be unavailable." in page.text
    assert 'data-csrf-token="' in page.text
    assert "data-user-id" not in page.text
    assert "X-User-Id" not in page.text


def test_successful_actions_refresh_and_preserve_feedback(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [_sample_listing(7, status="rejected")]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert page.text.count("window.location.reload()") == 1
    assert page.text.count("sessionStorage.setItem('listingActionMessage'") == 1
    assert "sessionStorage.getItem('listingActionMessage')" in page.text
    assert "sessionStorage.removeItem('listingActionMessage')" in page.text
    assert "showMessage(pendingMessage, 'success')" in page.text
    assert (
        "sessionStorage.setItem('listingActionMessage', successMessage)" in page.text
    )
    assert "showMessage(await errorMessage(response, fallback), 'danger')" in page.text
    assert "'X-CSRF-Token': csrfToken" in page.text
    assert "credentials: 'same-origin'" in page.text
    assert "Your session has expired. Please sign in again." in page.text
    assert "Your security token expired. Reload the page and try again." in page.text


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_edit_and_revision_javascript_preserves_fields_and_handles_errors():
    template = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "templates"
        / "alumni_dashboard.html"
    )
    harness = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const script = source.split('{% block subpage_scripts %}')[1]
  .split('<script>')[1].split('</script>')[0];

function element(dataset = {}) {
  const classes = new Set();
  return {
    dataset, value: '', textContent: '', disabled: false, listeners: {},
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle: (name, force) => force ? classes.add(name) : classes.delete(name)
    },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute() {},
    focus() {}
  };
}
const ids = {};
[
  'listingActionMessage', 'editListingPanel', 'editListingHeading',
  'editSubmitButton', 'editListingForm', 'editKind', 'editTitle', 'editBody',
  'editJobFields', 'editEventFields', 'cancelEdit'
].forEach(id => ids[id] = element());
ids.listingActionMessage.dataset.csrfToken = 'csrf-value';
const fields = {};
[
  'location', 'category', 'company_name', 'application_instructions',
  'application_deadline', 'faculty', 'department', 'event_date', 'event_time',
  'registration_information'
].forEach(name => fields[name] = element());
ids.editListingForm.elements = { namedItem: name => fields[name] };

const job = element({
  editId: '4', listing: JSON.stringify({
    kind: 'job', title: 'Job', body: 'Job description',
    location: 'Mona', category: 'Engineering', company_name: 'UWI Labs',
    application_instructions: 'Apply online', application_deadline: '2099-12-01',
    faculty: 'Science', department: 'Computing', event_date: '', event_time: '',
    registration_information: ''
  })
});
const event = element({
  reviseId: '8', listing: JSON.stringify({
    kind: 'event', title: 'Event', body: 'Event description',
    location: 'Hall', category: 'Workshop', company_name: '',
    application_instructions: '', application_deadline: '', faculty: '', department: '',
    event_date: '2099-10-12', event_time: '13:45:30',
    registration_information: '</script><img src=x onerror=alert(1)> & "quotes"'
  })
});
global.document = {
  getElementById: id => ids[id],
  querySelectorAll: selector => selector === '[data-edit-id], [data-revise-id]'
    ? [job, event] : []
};
const saved = {};
let reloads = 0;
global.sessionStorage = {
  getItem(key) { return saved[key] || null; },
  setItem(key, value) { saved[key] = value; },
  removeItem(key) { delete saved[key]; }
};
global.window = { location: { reload() { reloads += 1; } } };
let response = { status: 400, ok: false,
  json: async () => ({ detail: 'company_name is required for job listings' }) };
let calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, options, payload: JSON.parse(options.body) });
  return response;
};
vm.runInThisContext(script);
const submit = () => ids.editListingForm.listeners.submit({ preventDefault() {} });

(async () => {
  job.listeners.click();
  assert.strictEqual(fields.company_name.value, 'UWI Labs');
  assert.strictEqual(fields.application_deadline.value, '2099-12-01');
  assert.strictEqual(fields.department.value, 'Computing');
  assert.strictEqual(ids.editJobFields.disabled, false);
  assert.strictEqual(ids.editEventFields.disabled, true);
  fields.company_name.value = 'Changed company';
  await submit();
  assert.strictEqual(ids.listingActionMessage.textContent, 'Please enter the company name.');
  assert.strictEqual(fields.company_name.value, 'Changed company');
  assert.strictEqual(fields.application_deadline.value, '2099-12-01');
  assert.strictEqual(calls[0].url, '/api/boards/listings/4');
  assert.strictEqual(calls[0].options.method, 'PUT');
  assert.strictEqual(calls[0].options.headers['X-CSRF-Token'], 'csrf-value');
  assert.deepStrictEqual(calls[0].payload, {
    kind: 'job', title: 'Job', body: 'Job description', location: 'Mona',
    category: 'Engineering', company_name: 'Changed company',
    application_instructions: 'Apply online', application_deadline: '2099-12-01',
    faculty: 'Science', department: 'Computing'
  });
  response = { status: 400, ok: false,
    json: async () => ({ detail: 'application_instructions is required for job listings' }) };
  await submit();
  assert.strictEqual(ids.listingActionMessage.textContent, 'Please provide application instructions.');

  ids.editKind.value = 'event';
  ids.editKind.listeners.change();
  assert.strictEqual(ids.editJobFields.disabled, true);
  assert.strictEqual(ids.editEventFields.disabled, false);
  ids.editKind.value = 'job';
  ids.editKind.listeners.change();
  assert.strictEqual(fields.company_name.value, 'Changed company');

  event.listeners.click();
  assert.strictEqual(fields.event_date.value, '2099-10-12');
  assert.strictEqual(fields.event_time.value, '13:45:30');
  assert.strictEqual(fields.registration_information.value,
    '</script><img src=x onerror=alert(1)> & "quotes"');
  assert.strictEqual(fields.company_name.value, '');
  response = { status: 400, ok: false,
    json: async () => ({ detail: 'event_time must use HH:MM or HH:MM:SS' }) };
  await submit();
  assert.strictEqual(ids.listingActionMessage.textContent, 'Please enter a valid event time.');
  assert.strictEqual(calls[2].url, '/api/boards/listings/8/revision-request');
  assert.strictEqual(calls[2].options.method, 'POST');
  assert.deepStrictEqual(calls[2].payload, {
    kind: 'event', title: 'Event', body: 'Event description', location: 'Hall',
    category: 'Workshop', event_date: '2099-10-12', event_time: '13:45:30',
    registration_information: '</script><img src=x onerror=alert(1)> & "quotes"'
  });
  response = { status: 400, ok: false,
    json: async () => ({ detail: 'event_date is required for event listings' }) };
  await submit();
  assert.strictEqual(ids.listingActionMessage.textContent, 'Please enter the event date.');
  response = { status: 500, ok: false,
    json: async () => ({ detail: 'private technical exception' }) };
  await submit();
  assert.strictEqual(ids.listingActionMessage.textContent,
    'Unable to complete this action. Please try again.');
  assert.strictEqual(fields.registration_information.value,
    '</script><img src=x onerror=alert(1)> & "quotes"');
  response = { status: 403, ok: false, json: async () => ({ detail: 'CSRF failed' }) };
  await submit();
  assert.strictEqual(ids.listingActionMessage.textContent,
    'Your security token expired. Reload the page and try again.');
  response = { status: 200, ok: true };
  await submit();
  assert.strictEqual(reloads, 1);
  assert.ok(saved.listingActionMessage.startsWith('Revision request sent.'));
  job.listeners.click();
  await submit();
  assert.strictEqual(reloads, 2);
  assert.strictEqual(saved.listingActionMessage, 'Listing updated and sent for admin review.');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        ["node", "-e", harness, str(template)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
