"""Runtime checks for the merged notification bell script.

The existing bell test only inspects source text. This harness executes the
real JavaScript in Node with minimal DOM, fetch, and EventSource fakes so that
live delivery, reconnect, and duplicate suppression are observed behavior.
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

let fetchCalls = 0;
global.fetch = async () => {
  fetchCalls += 1;
  const notifications = fetchCalls === 1
    ? [
        { id: 2, user_id: 7, event_type: 'listing.published',
          message: 'Inbox older', created_at: '2026-11-24T00:00:00+00:00' },
        { id: 1, user_id: 7, event_type: 'campaign.sent',
          message: 'Inbox oldest', created_at: '2026-11-23T00:00:00+00:00' }
      ]
    : [
        { id: 3, user_id: 7, event_type: 'listing.published',
          message: 'Live newer', created_at: '2026-11-25T00:00:00+00:00' },
        { id: 2, user_id: 7, event_type: 'listing.published',
          message: 'Inbox older', created_at: '2026-11-24T00:00:00+00:00' },
        { id: 1, user_id: 7, event_type: 'campaign.sent',
          message: 'Inbox oldest', created_at: '2026-11-23T00:00:00+00:00' }
      ];
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

function notification(id, message) {
  return {
    id,
    user_id: 7,
    event_type: 'listing.published',
    message,
    created_at: '2026-11-25T00:00:00+00:00'
  };
}
function messages() {
  return elements.notificationList.children
    .map((row) => row.children[0].textContent);
}
function snapshot() {
  return {
    visible: messages(),
    count: elements.notificationCount.textContent,
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
    data: JSON.stringify(notification(1, 'Inbox duplicate')),
    lastEventId: '1'
  });
  await wait(5);
  const afterDuplicates = snapshot();

  sources[0].onerror();
  await wait(30);
  const afterError = snapshot();

  console.log(JSON.stringify({ initial, afterLive, afterDuplicates, afterError }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""


def test_script_delivers_reconnects_and_deduplicates_by_id() -> None:
    result = subprocess.run(
        ["node", "-e", _HARNESS, str(JS_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)

    assert report["initial"]["visible"] == ["Inbox older", "Inbox oldest"]
    assert report["initial"]["count"] == "2 notifications"
    assert report["initial"]["sources"] == 1
    assert report["initial"]["streamUrls"] == ["/api/notifications/stream"]

    assert report["afterLive"]["visible"][0] == "Live newer"
    assert len(report["afterLive"]["visible"]) == 3
    assert report["afterLive"]["count"] == "3 notifications"

    assert report["afterDuplicates"]["visible"] == report["afterLive"]["visible"]
    assert report["afterDuplicates"]["count"] == "3 notifications"

    assert report["afterError"]["fetchCalls"] == 2
    assert report["afterError"]["firstClosed"] is True
    assert report["afterError"]["sources"] == 2
    assert report["afterError"]["streamUrls"] == [
        "/api/notifications/stream",
        "/api/notifications/stream",
    ]
    assert 1000 in report["afterError"]["reconnectDelays"]
    assert report["afterError"]["visible"] == report["afterLive"]["visible"]
