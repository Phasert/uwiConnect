(function () {
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
  const controller = new AbortController();
  let loading = false;
  let loaded = false;

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

  function isUnread(item) {
    return item.unread === true || item.is_read === false;
  }

  function itemKey(item) {
    if (item.id !== undefined && item.id !== null) {
      return 'id:' + item.id;
    }
    return [item.event_type, item.created_at, item.message].join('|');
  }

  function createItem(item) {
    const row = document.createElement('li');
    row.className = 'app-notification-item';
    if (isUnread(item)) {
      row.classList.add('is-unread');
    }

    const message = document.createElement('div');
    message.className = 'app-notification-message small';
    message.textContent =
      typeof item.message === 'string' && item.message.trim()
        ? item.message
        : 'New notification';
    row.appendChild(message);

    const meta = document.createElement('div');
    meta.className = 'app-notification-meta text-body-secondary small';
    const parts = [];
    if (typeof item.event_type === 'string' && item.event_type) {
      parts.push(humanise(item.event_type));
    }
    const date = formatDate(item.created_at);
    if (date) {
      parts.push(date);
    }
    if (isUnread(item)) {
      parts.push('Unread');
    }
    meta.textContent = parts.join(' · ');
    if (meta.textContent) {
      row.appendChild(meta);
    }
    return row;
  }

  function updateIndicators(items, unreadCount) {
    if (unreadCount > 0) {
      badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
      badge.classList.remove('d-none');
      bell.setAttribute('aria-label', 'Notifications, ' + unreadCount + ' unread');
    } else {
      badge.textContent = '0';
      badge.classList.add('d-none');
      bell.setAttribute('aria-label', 'Notifications');
    }
    count.textContent = items.length
      ? items.length + (items.length === 1 ? ' notification' : ' notifications')
      : '';
  }

  function render(notifications) {
    const seen = new Set();
    const sorted = notifications.slice().sort(function (a, b) {
      if (typeof a.id === 'number' && typeof b.id === 'number') {
        return b.id - a.id;
      }
      const first = Date.parse(a.created_at || '');
      const second = Date.parse(b.created_at || '');
      if (!Number.isNaN(first) && !Number.isNaN(second)) {
        return second - first;
      }
      return 0;
    });

    clear(list);
    sorted.forEach(function (item) {
      const key = itemKey(item);
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      list.appendChild(createItem(item));
    });

    const unreadCount = sorted.filter(isUnread).length;
    updateIndicators(sorted, unreadCount);
    if (sorted.length === 0) {
      setStatus('You have no notifications yet.', false);
    } else {
      setStatus('', false);
    }
  }

  async function load() {
    if (loading || loaded) {
      return;
    }
    loading = true;
    setStatus('Loading notifications…', false);
    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
        signal: controller.signal
      });
      if (response.status === 401 || response.status === 403) {
        setStatus(
          'Notifications could not be loaded. If your session has expired, sign in again.',
          true
        );
        return;
      }
      if (!response.ok) {
        throw new Error('Request failed');
      }
      const payload = await response.json();
      if (!payload || !Array.isArray(payload.notifications)) {
        throw new Error('Unexpected response');
      }
      render(payload.notifications.filter(function (item) {
        return item && typeof item === 'object';
      }));
      loaded = true;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        return;
      }
      setStatus('Notifications are temporarily unavailable. Please try again later.', true);
    } finally {
      loading = false;
    }
  }

  window.addEventListener('pagehide', function () {
    controller.abort();
  });
  menu.addEventListener('show.bs.dropdown', function () {
    if (!loaded) {
      load();
    }
  });
  load();
})();
