(() => {
  const EMAIL_ERROR = "email must use an approved UWI domain";

  const emailInput = document.getElementById("email");
  const emailError = document.getElementById("email-error");
  if (!emailInput || !emailError) return;

  const form = emailInput.closest("form");
  if (!form) return;

  function approvedDomains() {
    try {
      const domains = JSON.parse(emailInput.dataset.approvedDomains || "[]");
      if (!Array.isArray(domains)) return [];
      return domains
        .map((domain) => String(domain).trim().toLowerCase())
        .filter(Boolean);
    } catch {
      return [];
    }
  }

  function isValidEmail(value, domains) {
    if (!domains.length) return true;
    const normalized = value.trim().toLowerCase();
    const parts = normalized.split("@");
    if (parts.length !== 2 || !parts[0] || !parts[1]) return false;
    return domains.includes(parts[1]);
  }

  function showEmailError() {
    emailInput.classList.add("is-invalid");
    emailInput.setAttribute("aria-invalid", "true");
    emailInput.setCustomValidity(EMAIL_ERROR);
    emailError.textContent = EMAIL_ERROR;
    emailError.classList.add("d-block");
  }

  function clearEmailError() {
    emailInput.classList.remove("is-invalid");
    emailInput.removeAttribute("aria-invalid");
    emailInput.setCustomValidity("");
    emailError.textContent = "";
    emailError.classList.remove("d-block");
  }

  function focusFirstInvalid() {
    const firstInvalid = form.querySelector(".is-invalid");
    if (firstInvalid) firstInvalid.focus();
  }

  emailInput.addEventListener("input", clearEmailError);

  emailInput.addEventListener("invalid", () => {
    showEmailError();
    emailInput.focus();
  });

  form.addEventListener("submit", (event) => {
    clearEmailError();
    if (isValidEmail(emailInput.value, approvedDomains())) return;

    showEmailError();
    event.preventDefault();
    emailInput.focus();
  });

  focusFirstInvalid();
})();
