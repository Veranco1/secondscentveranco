/*
 * SecondScent — kleine fetch-helper voor de server-gerenderde pagina's.
 *
 * De JSON-API (app/auth, app/listings, app/orders, app/disputes,
 * app/admin) verwacht bij elke state-changing request een geldig
 * X-CSRF-Token-header naast de sessioncookie (zie app/auth/routes.py).
 * Deze helper haalt dat token één keer op en cachet het in het geheugen
 * van de pagina; na login/register/logout wisselt de sessie (en dus het
 * token) altijd, dus die drie helpers verversen de cache zelf.
 */
(function () {
  let cachedToken = null;

  async function getCsrfToken(force) {
    if (cachedToken && !force) return cachedToken;
    const res = await fetch('/auth/csrf-token', { credentials: 'same-origin' });
    const data = await res.json();
    cachedToken = data.csrf_token;
    return cachedToken;
  }

  async function ssFetch(url, opts) {
    opts = opts || {};
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({}, opts.headers || {});
    const fetchOpts = Object.assign({ credentials: 'same-origin' }, opts);

    if (method !== 'GET') {
      headers['X-CSRF-Token'] = await getCsrfToken();
    }
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      fetchOpts.body = JSON.stringify(opts.json);
    }
    fetchOpts.headers = headers;

    let res = await fetch(url, fetchOpts);
    if (res.status === 400) {
      // Could be a stale CSRF token (e.g. session changed in another tab) —
      // refresh once and retry, exactly once, to avoid a retry loop.
      const clone = res.clone();
      let body;
      try { body = await clone.json(); } catch (e) { body = null; }
      if (body && body.error === 'invalid_csrf_token' && method !== 'GET') {
        headers['X-CSRF-Token'] = await getCsrfToken(true);
        fetchOpts.headers = headers;
        res = await fetch(url, fetchOpts);
      }
    }
    return res;
  }

  async function ssJson(url, opts) {
    const res = await ssFetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    return { ok: res.ok, status: res.status, data: data || {} };
  }

  function showFormError(el, message) {
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
  }
  function hideFormError(el) {
    if (!el) return;
    el.hidden = true;
    el.textContent = '';
  }

  window.SecondScent = { ssFetch, ssJson, getCsrfToken, showFormError, hideFormError };
})();
