/* ═══════════════════════════════════════════════
   TENANT WATCHDOG — 15s poll for tenant active state
   Included on every tenant admin page.

   On detection of is_active=false for the current tenant:
     1. Redirect to the tenant's login page with the slug in the URL
        (which triggers the disabled-UI we already shipped).
     2. The server-side _current_admin() also drops the session, so
        any action taken before this poll fires is still rejected.
   ═══════════════════════════════════════════════ */
(function () {
  'use strict';
  var INTERVAL_MS = 15000;

  function slugFromUrl() {
    // 1) Server-rendered slug on <body data-tenant-slug="..."> is the most
    //    reliable source — it survives navigations that drop the query
    //    string and does not depend on the session cookie.
    try {
      var b = document.body && document.body.dataset && document.body.dataset.tenantSlug;
      if (b) return b;
    } catch (_) {}
    // 2) Fall back to the URL for pages that carry ?__tenant=/?__preview=.
    try {
      var p = new URLSearchParams(location.search);
      return p.get('__tenant') || p.get('__preview') || '';
    } catch (_) { return ''; }
  }

  function redirectToLogin(slug) {
    var qs = slug ? ('?__tenant=' + encodeURIComponent(slug)) : '';
    // replace, not assign, so the back button doesn't bounce the user back
    location.replace('/admin/login' + qs);
  }

  function check() {
    var slug = slugFromUrl();
    var qs   = slug ? ('?__tenant=' + encodeURIComponent(slug)) : '';
    fetch('/api/tenant/is-active' + qs, {
      credentials: 'same-origin',
      cache: 'no-store',
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.active === false) {
          redirectToLogin(d.slug || slug);
        }
      })
      .catch(function () { /* network blip — try again next tick */ });
  }

  // First check 2s after load (avoid racing with page init), then every 15s.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      setTimeout(check, 2000);
      setInterval(check, INTERVAL_MS);
    });
  } else {
    setTimeout(check, 2000);
    setInterval(check, INTERVAL_MS);
  }
})();
