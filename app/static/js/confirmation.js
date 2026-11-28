function confirmAction(title, message, action) {
  const dialog = document.getElementById('confirmation-dialog');
  if (dialog.open) return Promise.resolve(false);

  document.getElementById('confirmation-title').textContent = title;
  document.getElementById('confirmation-message').textContent = message;
  document.getElementById('confirmation-action').textContent = action;
  dialog.returnValue = '';
  return new Promise(function (resolve) {
    dialog.addEventListener('close', function () {
      resolve(dialog.returnValue === 'confirm');
    }, { once: true });
    dialog.showModal();
  });
}

const confirmedForms = new WeakSet();
document.addEventListener('submit', async function (event) {
  const form = event.target;
  if (!form.matches('form[data-confirm-message]') || confirmedForms.has(form)) return;

  event.preventDefault();
  event.stopImmediatePropagation();
  const approved = await confirmAction(
    form.dataset.confirmTitle, form.dataset.confirmMessage, form.dataset.confirmAction
  );
  if (!approved) return;

  confirmedForms.add(form);
  try {
    form.requestSubmit(event.submitter);
  } finally {
    confirmedForms.delete(form);
  }
}, true);
