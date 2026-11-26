"""Runtime checks for the merged notification bell script.

The existing bell test only inspects source text. This harness executes the
real JavaScript in Node with minimal DOM, fetch, and EventSource fakes so that
live delivery, reconnect, duplicate suppression, and badge counts are observed
behavior.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "static"
    / "js"
    / "notifications.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required for this runtime check"
)

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const config = JSON.parse(process.argv[2] || '{}');

function FakeElement(id) {
  this.id = id;
  this.children = [];
  this.textContent = '';
  this.dataset = {};
  this.attributes = {};
  this.listeners = {};
  const classes = new Set();
  this.classList = {
    add: (...names) => names.forEach((name) => classes.add(name)),
    remove: (...names) => names.forEach((name) => classes.delete(name)),
    contains: (name) => classes.has(name),
    toggle: (name, force) => {
      if (force === undefined) {
        if (classes.has(name)) classes.delete(name);
        else classes.add(name);
      } else if (force) {
        classes.add(name);
      } else {
        classes.delete(name);
      }
    },
    toString: () => Array.from(classes).join(' ')
  };
}
Object.defineProperty(FakeElement.prototype, 'firstChild', {
  get() { return this.children[0] || null; }
});
FakeElement.prototype.appendChild = function (child) {
  this.children.push(child);
  return child;
};
FakeElement.prototype.removeChild = function (child) {
  const index = this.children.indexOf(child);
  if (index >= 0) this.children.splice(index, 1);
  return child;
};
FakeElement.prototype.setAttribute = function (name, value) {
  this.attributes[name] = value;
};
FakeElement.prototype.addEventListener = function (name, handler) {
  this.listeners[name] = handler;
};

const elements = {};
for (const id of [
  'notificationMenu',
  'notificationBell',
  'notificationStatus',
  'notificationList',
  'notificationBadge',
  'notificationCount'
]) {
  elements[id] = new FakeElement(id);
}
elements.notificationMenu.dataset = {
  notificationsUrl: '/api/notifications',
  streamUrl: '/api/notifications/stream',
  loginUrl: '/login'
};

global.document = {
  getElementById: (id) => elements[id] || null,
  createElement: (tag) => new FakeElement(tag)
};

const reconnectDelays = [];
global.window = {
  location: { pathname: '/app', assign() {} },
  addEventListener() {},
  setTimeout(handler, delay) {
    reconnectDelays.push(delay);
    return setTimeout(handler, Math.min(delay, 5));
  },
  clearTimeout(handle) { clearTimeout(handle); }
};

function notification(id, message, createdAt, eventType) {
  return {
    id,
    user_id: 7,
    event_type: eventType || 'listing.published',
    message,
    created_at: createdAt || '2026-11-25T00:00:00+00:00'
  };
}
function configuredNotifications(values, prefix) {
  return values.map(function (value) {
    if (value && typeof value === 'object') return value;
    return notification(value, prefix + ' ' + value);
  });
}
const defaultInitial = [
  notification(2, 'Inbox older', '2026-11-24T00:00:00+00:00'),
  notification(1, 'Inbox oldest', '2026-11-23T00:00:00+00:00', 'campaign.sent')
];
const defaultReconciled = [
  notification(3, 'Live newer', '2026-11-25T00:00:00+00:00'),
  notification(2, 'Inbox older', '2026-11-24T00:00:00+00:00'),
  notification(1, 'Inbox oldest', '2026-11-23T00:00:00+00:00', 'campaign.sent')
];
const initialNotifications = Array.isArray(config.initial)
  ? configuredNotifications(config.initial, 'Inbox')
  : defaultInitial;
const reconciledNotifications = Array.isArray(config.reconcile)
  ? configuredNotifications(config.reconcile, 'Reconciled')
  : defaultReconciled;
const firstInitialId = initialNotifications.length
  ? initialNotifications[0].id
  : 1;

let fetchCalls = 0;
global.fetch = async () => {
  fetchCalls += 1;
  if (fetchCalls > 1 && config.failReconcile) {
    return { status: 500, ok: false, json: async () => ({}) };
  }
  const notifications = fetchCalls === 1 ? initialNotifications : reconciledNotifications;
  return { status: 200, ok: true, json: async () => ({ notifications }) };
};

const sources = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    sources.push(this);
  }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  emit(name, payload) { this.listeners[name](payload); }
  close() { this.closed = true; }
}
global.EventSource = FakeEventSource;

function messages() {
  return elements.notificationList.children
    .map((row) => row.children[0].textContent);
}
function snapshot() {
  return {
    visible: messages(),
    count: elements.notificationCount.textContent,
    badge: elements.notificationBadge.textContent,
    badgeHidden: elements.notificationBadge.classList.contains('d-none'),
    label: elements.notificationBell.attributes['aria-label'],
    status: elements.notificationStatus.textContent,
    sources: sources.length,
    fetchCalls,
    firstClosed: sources.length ? sources[0].closed : null,
    streamUrls: sources.map((source) => source.url),
    reconnectDelays: reconnectDelays.slice()
  };
}

vm.runInThisContext(source, { filename: 'notifications.js' });

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
(async () => {
  await wait(20);
  const initial = snapshot();

  sources[0].emit('notification', {
    data: JSON.stringify(notification(3, 'Live newer')),
    lastEventId: '3'
  });
  await wait(5);
  const afterLive = snapshot();

  sources[0].emit('notification', {
    data: JSON.stringify(notification(3, 'Live newer duplicate')),
    lastEventId: '3'
  });
  sources[0].emit('notification', {
    data: JSON.stringify(notification(firstInitialId, 'Inbox duplicate')),
    lastEventId: String(firstInitialId)
  });
  await wait(5);
  const afterDuplicates = snapshot();

  elements.notificationMenu.listeners['show.bs.dropdown']();
  await wait(5);
  const afterDropdown = snapshot();

  sources[0].onerror();
  await wait(30);
  const afterError = snapshot();

  console.log(JSON.stringify({ initial, afterLive, afterDuplicates, afterDropdown, afterError }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""


def _run_scenario(config: dict[str, object] | None = None) -> dict:
    command = ["node", "-e", _HARNESS, str(JS_PATH)]
    if config is not None:
        command.append(json.dumps(config))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_script_delivers_reconnects_and_deduplicates_by_id() -> None:
    report = _run_scenario()

    assert report["initial"]["visible"] == ["Inbox older", "Inbox oldest"]
    assert report["initial"]["count"] == "2 notifications"
    assert report["initial"]["badge"] == "2"
    assert report["initial"]["badgeHidden"] is False
    assert report["initial"]["label"] == "Notifications, 2 total"
    assert report["initial"]["sources"] == 1
    assert report["initial"]["streamUrls"] == ["/api/notifications/stream"]

    assert report["afterLive"]["visible"][0] == "Live newer"
    assert len(report["afterLive"]["visible"]) == 3
    assert report["afterLive"]["count"] == "3 notifications"
    assert report["afterLive"]["badge"] == "3"
    assert report["afterLive"]["badgeHidden"] is False
    assert report["afterLive"]["label"] == "Notifications, 3 total"

    assert report["afterDuplicates"]["visible"] == report["afterLive"]["visible"]
    assert report["afterDuplicates"]["count"] == "3 notifications"
    assert report["afterDuplicates"]["badge"] == "3"
    assert report["afterDuplicates"]["label"] == "Notifications, 3 total"

    assert report["afterDropdown"]["badge"] == "3"
    assert report["afterDropdown"]["fetchCalls"] == 1
    assert report["afterDropdown"]["visible"] == report["afterLive"]["visible"]

    assert report["afterError"]["fetchCalls"] == 2
    assert report["afterError"]["firstClosed"] is True
    assert report["afterError"]["sources"] == 2
    assert report["afterError"]["streamUrls"] == [
        "/api/notifications/stream",
        "/api/notifications/stream",
    ]
    assert 1000 in report["afterError"]["reconnectDelays"]
    assert report["afterError"]["visible"] == report["afterLive"]["visible"]
    assert report["afterError"]["badge"] == "3"
    assert report["afterError"]["badgeHidden"] is False
    assert report["afterError"]["label"] == "Notifications, 3 total"


def test_zero_notifications_hide_the_badge() -> None:
    initial = _run_scenario({"initial": []})["initial"]

    assert initial["visible"] == []
    assert initial["count"] == ""
    assert initial["badge"] == "0"
    assert initial["badgeHidden"] is True
    assert initial["label"] == "Notifications"
    assert initial["status"] == "You have no notifications yet."


@pytest.mark.parametrize(
    ("total", "badge"),
    [
        (1, "1"),
        (99, "99"),
        (120, "99+"),
    ],
)
def test_badge_shows_full_count_until_99_then_caps(total: int, badge: str) -> None:
    initial = _run_scenario({"initial": list(range(1, total + 1))})["initial"]

    assert len(initial["visible"]) == total
    assert initial["badge"] == badge
    assert initial["badgeHidden"] is False
    assert initial["label"] == f"Notifications, {total} total"
    suffix = "" if total == 1 else "s"
    assert initial["count"] == f"{total} notification{suffix}"


def test_opening_dropdown_does_not_reset_the_counter() -> None:
    report = _run_scenario()

    after_live = report["afterLive"]
    after_dropdown = report["afterDropdown"]

    assert after_live["badge"] == "3"
    assert after_dropdown["badge"] == "3"
    assert after_dropdown["badgeHidden"] is False
    assert after_dropdown["label"] == "Notifications, 3 total"
    assert after_dropdown["count"] == "3 notifications"
    assert after_dropdown["visible"] == after_live["visible"]
    assert after_dropdown["fetchCalls"] == after_live["fetchCalls"] == 1


def test_reconciliation_updates_the_badge_count() -> None:
    report = _run_scenario({"reconcile": [4, 3, 2, 1]})
    after_error = report["afterError"]

    assert report["initial"]["badge"] == "2"
    assert report["afterLive"]["badge"] == "3"
    assert report["afterDuplicates"]["badge"] == "3"
    assert after_error["fetchCalls"] == 2
    assert after_error["badge"] == "4"
    assert after_error["badgeHidden"] is False
    assert after_error["label"] == "Notifications, 4 total"
    assert after_error["count"] == "4 notifications"
    assert after_error["visible"] == [
        "Reconciled 4",
        "Reconciled 3",
        "Reconciled 2",
        "Reconciled 1",
    ]


def test_failed_reconciliation_retains_notifications_and_count() -> None:
    report = _run_scenario({"failReconcile": True})
    after_error = report["afterError"]

    assert after_error["fetchCalls"] == 2
    assert after_error["firstClosed"] is True
    assert after_error["visible"] == report["afterLive"]["visible"]
    assert after_error["badge"] == "3"
    assert after_error["badgeHidden"] is False
    assert after_error["label"] == "Notifications, 3 total"
    assert after_error["count"] == "3 notifications"
    assert "temporarily unavailable" in after_error["status"]
