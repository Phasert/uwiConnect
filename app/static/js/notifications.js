(function () {
  'use strict';

  const menu = document.getElementById('notificationMenu');
  if (!menu) {
    return;
  }

  const bell = document.getElementById('notificationBell');
  const status = document.getElementById('notificationStatus');
  const list = document.getElementById('notificationList');
  const badge = document.getElementById('notificationBadge');
  const count = document.getElementById('notificationCount');
  const url = menu.dataset.notificationsUrl || '/api/notifications';
  const streamUrl = menu.dataset.streamUrl || '/api/notifications/stream';
  const loginUrl = menu.dataset.loginUrl || '/login';
  const controller = new AbortController();
  const items = new Map();

  const INITIAL_RECONNECT_DELAY_MS = 1000;
  const MAX_RECONNECT_DELAY_MS = 15000;

  let stream = null;
  let reconnectTimer = null;
  let reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
  let loading = false;
  let loaded = false;
  let handlingError = false;
  let pageHidden = false;
  let stopped = false;

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle('text-danger', Boolean(isError));
    status.classList.toggle('text-body-secondary', !isError);
  }

  function clear(element) {
    while (element.firstChild) {
      element.removeChild(element.firstChild);
    }
  }

  function humanise(value) {
    return String(value)
      .replace(/[._]/g, ' ')
      .replace(/\b\w/g, function (letter) {
        return letter.toUpperCase();
      });
  }

  function formatDate(value) {
    if (typeof value !== 'string' || !value) {
      return '';
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleString();
  }

  function notificationId(item, lastEventId) {
    if (!item || typeof item !== 'object') {
      return null;
    }
    if (item.id !== undefined && item.id !== null && String(item.id).trim()) {
      return String(item.id).trim();
    }
    if (lastEventId !== undefined && lastEventId !== null && String(lastEventId).trim()) {
      return String(lastEventId).trim();
    }
    return null;
  }

  function normalise(item, lastEventId) {
    const id = notificationId(item, lastEventId);
    if (!id) {
      return null;
    }
    return {
      id: id,
      user_id: item.user_id,
      event_type: typeof item.event_type === 'string' ? item.event_type : '',
      message:
        typeof item.message === 'string' && item.message.trim()
          ? item.message
          : 'New notification',
      created_at: typeof item.created_at === 'string' ? item.created_at : ''
    };
  }

  function itemKey(item) {
    return 'id:' + item.id;
  }

  function createItem(item) {
    const row = document.createElement('li');
    row.className = 'app-notification-item';

    const message = document.createElement('div');
    message.className = 'app-notification-message small';
    message.textContent = item.message;
    row.appendChild(message);

    const meta = document.createElement('div');
    meta.className = 'app-notification-meta text-body-secondary small';
    const parts = [];
    if (item.event_type) {
      parts.push(humanise(item.event_type));
    }
    const date = formatDate(item.created_at);
    if (date) {
      parts.push(date);
    }
    meta.textContent = parts.join(' · ');
    if (meta.textContent) {
      row.appendChild(meta);
    }
    return row;
  }

  function compareItems(a, b) {
    const firstId = Number(a.id);
    const secondId = Number(b.id);
    if (Number.isFinite(firstId) && Number.isFinite(secondId) && firstId !== secondId) {
      return secondId - firstId;
    }
    const firstDate = Date.parse(a.created_at || '');
    const secondDate = Date.parse(b.created_at || '');
    if (!Number.isNaN(firstDate) && !Number.isNaN(secondDate) && firstDate !== secondDate) {
      return secondDate - firstDate;
    }
    return 0;
  }

  function updateIndicators(notifications) {
    const total = notifications.length;
    badge.textContent = '0';
    badge.classList.add('d-none');
    bell.setAttribute(
      'aria-label',
      total ? 'Notifications, ' + total + ' total' : 'Notifications'
    );
    count.textContent = total
      ? total + (total === 1 ? ' notification' : ' notifications')
      : '';
  }

  function render(notifications) {
    const sorted = Array.from(notifications).sort(compareItems);
    clear(list);

    const seen = new Set();
    sorted.forEach(function (item) {
      const key = itemKey(item);
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      list.appendChild(createItem(item));
    });

    updateIndicators(sorted);
    if (sorted.length === 0) {
      setStatus('You have no notifications yet.', false);
    } else {
      setStatus('', false);
    }
  }

  function renderItems() {
    render(items.values());
  }

  function applyInbox(payload) {
    if (!payload || !Array.isArray(payload.notifications)) {
      throw new Error('Unexpected response');
    }
    items.clear();
    payload.notifications.forEach(function (candidate) {
      const item = normalise(candidate);
      if (item) {
        items.set(item.id, item);
      }
    });
    loaded = true;
    reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
    renderItems();
  }

  function inboxRequest() {
    return fetch(url, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal
    });
  }

  function closeStream() {
    if (stream) {
      stream.close();
      stream = null;
    }
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function redirectToLogin() {
    stopped = true;
    closeStream();
    if (!window.location) {
      return;
    }
    if (window.location.pathname && window.location.pathname === '/login') {
      return;
    }
    if (typeof window.location.assign === 'function') {
      window.location.assign(loginUrl);
      return;
    }
    window.location.href = loginUrl;
  }

  function scheduleReconnect() {
    if (pageHidden || stopped || reconnectTimer) {
      return;
    }
    reconnectTimer = window.setTimeout(function () {
      reconnectTimer = null;
      openStream();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
  }

  function openStream() {
    if (pageHidden || stopped || stream) {
      return;
    }
    stream = new EventSource(streamUrl);
    stream.addEventListener('notification', function (event) {
      reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      const item = normalise(payload, event.lastEventId);
      if (!item || items.has(item.id)) {
        return;
      }
      items.set(item.id, item);
      renderItems();
    });
    stream.onerror = function () {
      handleStreamError();
    };
  }

  async function handleStreamError() {
    if (handlingError) {
      return;
    }
    handlingError = true;
    closeStream();
    try {
      const response = await inboxRequest();
      if (response.status === 401 || response.status === 403) {
        redirectToLogin();
        return;
      }
      if (!response.ok) {
        throw new Error('Request failed');
      }
      applyInbox(await response.json());
    } catch (error) {
      if (error && error.name === 'AbortError') {
        return;
      }
      setStatus('Notifications are temporarily unavailable. Reconnecting…', true);
    } finally {
      handlingError = false;
    }
    scheduleReconnect();
  }

  async function load() {
    if (loading) {
      return;
    }
    loading = true;
    setStatus('Loading notifications…', false);
    try {
      const response = await inboxRequest();
      if (response.status === 401 || response.status === 403) {
        redirectToLogin();
        return;
      }
      if (!response.ok) {
        throw new Error('Request failed');
      }
      applyInbox(await response.json());
    } catch (error) {
      if (error && error.name === 'AbortError') {
        return;
      }
      setStatus(
        'Notifications are temporarily unavailable. Please try again later.',
        true
      );
    } finally {
      loading = false;
    }
  }

  window.addEventListener('pagehide', function () {
    pageHidden = true;
    controller.abort();
    closeStream();
  });

  menu.addEventListener('show.bs.dropdown', function () {
    if (!loaded) {
      load();
    }
  });

  load().then(function () {
    openStream();
  });
})();
