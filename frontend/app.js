/* ═══════════════════════════════════════════════
   SIMON FRESH WATER — Frontend Logic
   ═══════════════════════════════════════════════ */

const API = window.location.origin;

/* ─── Helpers ─── */
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}
function fmt(n) {
  return Number(n).toLocaleString('en-KE', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-KE', {
    day: '2-digit', month: 'short', year: 'numeric',
  });
}
function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-KE', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}
function badgeClass(status) {
  const map = {
    CLEARED: 'badge-cleared', PREPAYMENT: 'badge-prepaid',
    DUE: 'badge-due', OVERDUE: 'badge-overdue',
    OVERDUE_APPROACHING: 'badge-approaching',
  };
  return map[status] || 'badge-none';
}
function badge(status, label) {
  return `<span class="badge ${badgeClass(status)}">${esc(label)}</span>`;
}

/* ─── Global state ─── */
let AUTH_STATE = { authenticated: false, username: null, setup_required: false };
let CURRENT_CONSUMER = null;
let CURRENT_PAYMENTS = [];

async function refreshAuth() {
  try {
    const r = await fetch('/api/admin/whoami', { credentials: 'same-origin' });
    AUTH_STATE = await r.json();
  } catch {
    AUTH_STATE = { authenticated: false, username: null, setup_required: false };
  }
  return AUTH_STATE;
}

function requireLoginRedirect() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/admin/login?next=${next}`;
}

async function adminFetch(url, options = {}) {
  options.credentials = 'same-origin';
  options.headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  const r = await fetch(url, options);
  if (r.status === 401) {
    requireLoginRedirect();
    throw new Error('Redirecting to login…');
  }
  return r;
}

/* ═══════════════════════════════════════════════
   AUTH TAB
   ═══════════════════════════════════════════════ */
function renderAuthTab() {
  const btn = document.getElementById('authTab');
  const label = document.getElementById('authTabLabel');
  const icon = document.getElementById('authTabIcon');
  if (!btn) return;

  if (AUTH_STATE.authenticated) {
    btn.classList.remove('tab-login');
    btn.classList.add('tab-logout');
    label.textContent = 'Logout';
    icon.textContent = '🚪';
    btn.onclick = async () => {
      if (!confirm('Log out of the admin panel?')) return;
      try { await fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' }); } catch {}
      location.href = '/';
    };
  } else {
    btn.classList.remove('tab-logout');
    btn.classList.add('tab-login');
    label.textContent = 'Login';
    icon.textContent = '🔐';
    btn.onclick = () => {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/admin/login?next=${next}`;
    };
  }
}

/* ═══════════════════════════════════════════════
   HOME PAGE
   ═══════════════════════════════════════════════ */
async function initHomePage() {
  const input = document.getElementById('searchInput');
  const btn = document.getElementById('searchBtn');
  const loader = document.getElementById('searchLoader');
  const errorBox = document.getElementById('searchError');
  const resultsBox = document.getElementById('resultsContainer');
  const resultsList = document.getElementById('resultsList');

  await refreshAuth();
  renderAuthTab();

  let debounce;

  function doSearch() {
    const q = input.value.trim();
    if (q.length < 2) {
      errorBox.textContent = 'Please enter at least 2 characters.';
      errorBox.classList.remove('hidden');
      return;
    }

    errorBox.classList.add('hidden');
    resultsBox.classList.add('hidden');
    loader.classList.remove('hidden');
    btn.disabled = true;

    fetch(`${API}/api/search?q=${encodeURIComponent(q)}`, { credentials: 'same-origin' })
      .then(async (r) => {
        if (r.status === 401) return { __unauthenticated: true };
        if (r.status === 429) {
          const d = await r.json().catch(() => ({}));
          return { __ratelimited: true, message: d.error || 'Too many requests. Slow down.' };
        }
        return r.json();
      })
      .then((data) => {
        loader.classList.add('hidden');
        btn.disabled = false;

        if (data.__unauthenticated) {
          errorBox.innerHTML =
            '🔐 Please <a href="/admin/login?next=' +
            encodeURIComponent(location.pathname) +
            '" style="color:#fff;text-decoration:underline;font-weight:700;">log in</a> to access consumer records.';
          errorBox.classList.remove('hidden');
          return;
        }
        if (data.__ratelimited) {
          errorBox.textContent = '⏳ ' + data.message;
          errorBox.classList.remove('hidden');
          return;
        }

        const results = data.results || [];
        if (results.length === 0) {
          errorBox.textContent = 'No matching consumer found.';
          errorBox.classList.remove('hidden');
          return;
        }
        resultsList.innerHTML = results.map((c) => `
          <a class="result-item ${c.is_active ? '' : 'result-item-terminated'}"
             href="/consumer?id=${c.id}">
            <div>
              <div class="result-name">
                ${esc(c.cust_name)}
                ${c.is_active ? '' : '<span class="terminated-tag">TERMINATED</span>'}
              </div>
              <div class="result-acc">Acc: ${esc(c.meter_acc_no)} · ${esc(c.acc_name)}</div>
            </div>
            <div class="result-right">${badge(c.status, c.status_label)}</div>
          </a>`).join('');
        resultsBox.classList.remove('hidden');
      })
      .catch(() => {
        loader.classList.add('hidden');
        btn.disabled = false;
        errorBox.textContent = 'Something went wrong. Please try again.';
        errorBox.classList.remove('hidden');
      });
  }

  btn.addEventListener('click', doSearch);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  input.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(doSearch, 450);
  });

  document.getElementById('openNewCustomerBtn')
    .addEventListener('click', guardAndOpen(openNewCustomerModal));
  document.getElementById('openTerminateBtn')
    .addEventListener('click', guardAndOpen(openTerminateModal));

  const termInput = document.getElementById('termSearchInput');
  let termDebounce;
  termInput.addEventListener('input', () => {
    clearTimeout(termDebounce);
    termDebounce = setTimeout(runTerminateSearch, 400);
  });
}

function guardAndOpen(fn) {
  return async () => {
    if (!AUTH_STATE.authenticated) {
      const s = await refreshAuth();
      if (!s.authenticated) { requireLoginRedirect(); return; }
      renderAuthTab();
    }
    fn();
  };
}

/* ═══════════════════════════════════════════════
   NEW CUSTOMER MODAL
   ═══════════════════════════════════════════════ */
function openNewCustomerModal() {
  document.getElementById('newCustomerModal').classList.remove('hidden');
  document.getElementById('newCustomerMsg').classList.add('hidden');
  document.getElementById('newCustomerForm').reset();
  document.getElementById('ncInitialReading').value = '0';
}
function closeNewCustomerModal() {
  document.getElementById('newCustomerModal').classList.add('hidden');
}
async function submitNewCustomer() {
  const btn = document.getElementById('createCustomerBtn');
  const msg = document.getElementById('newCustomerMsg');

  const payload = {
    cust_name:    document.getElementById('ncCustName').value.trim(),
    acc_name:     document.getElementById('ncAccName').value.trim(),
    meter_acc_no: document.getElementById('ncMeterAcc').value.trim(),
    meter_initial_reading_m3: parseFloat(document.getElementById('ncInitialReading').value || '0'),
    contact:      document.getElementById('ncContact').value.trim(),
    email:        document.getElementById('ncEmail').value.trim(),
    address:      document.getElementById('ncAddress').value.trim(),
    latitude:     document.getElementById('ncLatitude').value.trim() || null,
    longitude:    document.getElementById('ncLongitude').value.trim() || null,
  };

  if (!payload.cust_name || !payload.acc_name || !payload.meter_acc_no || !payload.contact) {
    msg.className = 'alert alert-error';
    msg.textContent = 'Customer Name, Account Name, Meter No., and Contact are required.';
    msg.classList.remove('hidden');
    return;
  }

  btn.disabled = true; btn.textContent = 'Creating…';
  msg.classList.add('hidden');

  try {
    const r = await adminFetch(`${API}/api/admin/consumer`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      msg.className = 'alert alert-success';
      msg.textContent = `✅ ${data.consumer.cust_name} (${data.consumer.meter_acc_no}) created.`;
      msg.classList.remove('hidden');
      setTimeout(() => {
        closeNewCustomerModal();
        location.href = `/consumer?id=${data.consumer.id}`;
      }, 1200);
    } else {
      msg.className = 'alert alert-error';
      msg.textContent = data.error || 'Failed.';
      msg.classList.remove('hidden');
      btn.disabled = false; btn.textContent = 'Create Customer';
    }
  } catch (e) {
    if (e.message && e.message.includes('Redirecting')) return;
    msg.className = 'alert alert-error';
    msg.textContent = 'Network error.';
    msg.classList.remove('hidden');
    btn.disabled = false; btn.textContent = 'Create Customer';
  }
}

/* ═══════════════════════════════════════════════
   TERMINATE MODAL
   ═══════════════════════════════════════════════ */
function openTerminateModal() {
  document.getElementById('terminateModal').classList.remove('hidden');
  document.getElementById('termSearchInput').value = '';
  document.getElementById('termResultsList').innerHTML = '';
  document.getElementById('terminateMsg').classList.add('hidden');
}
function closeTerminateModal() {
  document.getElementById('terminateModal').classList.add('hidden');
}

function runTerminateSearch() {
  const q = document.getElementById('termSearchInput').value.trim();
  const loader = document.getElementById('termSearchLoader');
  const list = document.getElementById('termResultsList');
  const msg = document.getElementById('terminateMsg');

  if (q.length < 2) { list.innerHTML = ''; return; }
  loader.classList.remove('hidden');
  msg.classList.add('hidden');

  fetch(`${API}/api/search?q=${encodeURIComponent(q)}`, { credentials: 'same-origin' })
    .then(async (r) => {
      if (r.status === 401) return { __unauthenticated: true };
      return r.json();
    })
    .then((data) => {
      loader.classList.add('hidden');
      if (data.__unauthenticated) { requireLoginRedirect(); return; }

      const results = data.results || [];
      if (!results.length) {
        list.innerHTML = '<p class="form-hint-small">No matches.</p>';
        return;
      }
      list.innerHTML = results.map((c) => `
        <div class="term-result ${c.is_active ? '' : 'term-result-terminated'}">
          <div>
            <div class="result-name">${esc(c.cust_name)}</div>
            <div class="result-acc">Acc: ${esc(c.meter_acc_no)} · ${esc(c.acc_name)}</div>
          </div>
          <div>
            ${c.is_active
              ? `<button class="btn-admin btn-delete" data-id="${c.id}"
                         data-name="${esc(c.cust_name)}" type="button">Terminate</button>`
              : `<span class="terminated-tag">TERMINATED</span>
                 <a class="btn-admin btn-reactivate" href="/consumer?id=${c.id}">Open</a>`}
          </div>
        </div>`).join('');

      list.querySelectorAll('.btn-delete[data-id]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          const id = e.currentTarget.getAttribute('data-id');
          const name = e.currentTarget.getAttribute('data-name');
          if (!confirm(`Terminate ${name}? This preserves all data and stops reminders.`)) return;
          terminateCustomer(id, name);
        });
      });
    })
    .catch(() => {
      loader.classList.add('hidden');
      msg.className = 'alert alert-error';
      msg.textContent = 'Search failed.';
      msg.classList.remove('hidden');
    });
}

async function terminateCustomer(id, name) {
  const msg = document.getElementById('terminateMsg');
  try {
    const r = await adminFetch(`${API}/api/admin/consumer/${id}/terminate`, { method: 'POST' });
    const data = await r.json();
    if (r.ok && data.ok) {
      msg.className = 'alert alert-success';
      msg.textContent = `✅ ${name} terminated.`;
      msg.classList.remove('hidden');
      runTerminateSearch();
    } else {
      msg.className = 'alert alert-error';
      msg.textContent = data.error || 'Termination failed.';
      msg.classList.remove('hidden');
    }
  } catch (e) {
    if (e.message && e.message.includes('Redirecting')) return;
    msg.className = 'alert alert-error';
    msg.textContent = 'Network error.';
    msg.classList.remove('hidden');
  }
}

/* ═══════════════════════════════════════════════
   CONSUMER PAGE
   ═══════════════════════════════════════════════ */
async function initConsumerPage(consumerId) {
  const loader = document.getElementById('pageLoader');
  if (!consumerId) { loader.textContent = 'No consumer selected.'; return; }

  await refreshAuth();

  fetch(`${API}/api/consumer/${consumerId}`, { credentials: 'same-origin' })
    .then((r) => r.json())
    .then((data) => {
      loader.classList.add('hidden');
      CURRENT_CONSUMER = data.consumer;
      CURRENT_PAYMENTS = data.payments || [];

      const c = data.consumer;
      const s = data.overall_status;
      const readings = data.readings || [];
      const isActive = c.is_active !== false;
      const isAdmin = AUTH_STATE.authenticated;

      if (!isActive) {
        document.getElementById('terminatedBanner').classList.remove('hidden');
        document.getElementById('terminatedAtText').textContent =
          c.terminated_at ? `Terminated on ${fmtDateTime(c.terminated_at)}` : '';
        document.getElementById('adminActions').classList.remove('hidden');
        document.getElementById('readingFormCard').classList.add('hidden');
        document.getElementById('notifyCard').classList.add('hidden');
        document.getElementById('paymentCard').classList.add('hidden');

        document.getElementById('reactivateBtn')
          .addEventListener('click', () => reactivateConsumer(c.id, c.cust_name));
        document.getElementById('deleteBtn')
          .addEventListener('click', () => deleteConsumer(c.id, c.cust_name));
      }

      /* Overview */
      document.getElementById('overviewCard').classList.remove('hidden');
      document.getElementById('custNameHeading').textContent = c.cust_name;
      document.getElementById('accName').textContent = c.acc_name;
      document.getElementById('meterAccNo').textContent = c.meter_acc_no;
      document.getElementById('contact').textContent = c.contact;
      document.getElementById('email').textContent = c.email || '—';
      document.getElementById('custNameValue').textContent = c.cust_name;
      document.getElementById('latitude').textContent = c.latitude != null ? c.latitude : '—';
      document.getElementById('longitude').textContent = c.longitude != null ? c.longitude : '—';
      document.getElementById('initialReading').textContent =
        Number(c.meter_initial_reading_m3 || 0).toFixed(2) + ' M³';

      const addrEl = document.getElementById('address');
      const mapsUrl = buildMapsUrl(c);
      if (mapsUrl) {
        addrEl.innerHTML = `<a href="${mapsUrl}" target="_blank" rel="noopener"
                                class="map-link">${esc(c.acc_name || c.address || 'View on map')}</a>
                            ${c.address ? `<span class="addr-small"> · ${esc(c.address)}</span>` : ''}`;
      } else {
        addrEl.textContent = c.address || '—';
      }

      const badgeEl = document.getElementById('overallBadge');
      if (!isActive) {
        badgeEl.className = 'badge badge-terminated';
        badgeEl.textContent = 'Terminated';
      } else {
        badgeEl.className = 'badge ' + badgeClass(s.status);
        badgeEl.textContent = s.label;
      }

      const sum = document.getElementById('statusSummary');
      let html = `<p><strong>Overall Status:</strong> ${isActive ? esc(s.label) : 'Terminated'}</p>`;
      if (s.total_due > 0) html += `<p><strong>Outstanding:</strong> KES ${fmt(s.total_due)}</p>`;
      if (s.total_prepaid > 0) html += `<p><strong>Prepaid Credit:</strong> KES ${fmt(s.total_prepaid)}</p>`;
      if (s.last_reading_date)
        html += `<p><strong>Last Reading:</strong> ${fmtDate(s.last_reading_date)} (${s.age_days} days ago)</p>`;
      sum.innerHTML = html;

      /* Edit toggle — only for logged-in admins */
      const editBtn = document.getElementById('editToggleBtn');
      if (!isAdmin) {
        editBtn.style.display = 'none';
      } else {
        editBtn.addEventListener('click', toggleEditMode);
      }

      /* Readings */
      document.getElementById('readingsCard').classList.remove('hidden');
      document.getElementById('readingsBody').innerHTML = readings.map((r) => `
        <tr>
          <td>${fmtDate(r.reading_date)}</td>
          <td>${Number(r.reading_m3).toFixed(2)}</td>
          <td>${Number(r.consumption_m3 || 0).toFixed(2)}</td>
          <td>${fmt(r.amount_kes)}</td>
          <td>${fmt(r.amount_paid)}</td>
          <td>${fmt(r.balance)}</td>
          <td><a href="/bill?id=${r.id}">${esc(r.bill_status)} →</a></td>
        </tr>`).join('');

      /* Payment card (admins only, active consumers) */
      if (isActive && isAdmin) {
        document.getElementById('paymentCard').classList.remove('hidden');
        document.getElementById('paymentForm').addEventListener('submit', (e) => {
          e.preventDefault();
          submitPayment(consumerId);
        });
        renderPaymentHistory();

        /* Show download button ONCE if a payment was just recorded.
           Reading + clearing here ensures the button disappears on the
           NEXT refresh — it only survives the single post-payment reload. */
        const pid  = sessionStorage.getItem('pendingReceiptPid');
        const rno  = sessionStorage.getItem('pendingReceiptNo');
        const pcid = sessionStorage.getItem('pendingReceiptCid');
        if (pid && String(pcid) === String(consumerId)) {
          showDownloadReceiptButton(pid, rno);
          sessionStorage.removeItem('pendingReceiptPid');
          sessionStorage.removeItem('pendingReceiptNo');
          sessionStorage.removeItem('pendingReceiptCid');
        }
      }

      /* Notify + reading form */
      if (isActive) {
        const notifyCard = document.getElementById('notifyCard');
        const outstanding = ['DUE', 'OVERDUE', 'OVERDUE_APPROACHING'].includes(s.status);
        const available = data.available_channels || [];
        if (outstanding && available.length > 0 && isAdmin) {
          notifyCard.classList.remove('hidden');
          const smsBtn = document.getElementById('sendSmsBtn');
          const emailBtn = document.getElementById('sendEmailBtn');
          if (!available.includes('sms'))   smsBtn.style.display = 'none';
          if (!available.includes('email')) emailBtn.style.display = 'none';
          wireNotifyButtons(consumerId);
        }

        document.getElementById('readingFormCard').classList.remove('hidden');
        document.getElementById('readingForm').addEventListener('submit', (e) => {
          e.preventDefault();
          const val = document.getElementById('readingInput').value;
          const msgEl = document.getElementById('readingFormMsg');
          const submitBtn = document.getElementById('readingSubmitBtn');
          submitBtn.disabled = true;

          fetch(`${API}/api/reading`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              consumer_id: parseInt(consumerId, 10),
              reading_m3: parseFloat(val),
            }),
          })
            .then((r) => r.json())
            .then((res) => {
              submitBtn.disabled = false;
              if (res.error) {
                msgEl.className = 'alert alert-error';
                msgEl.textContent = res.error;
                msgEl.classList.remove('hidden');
                return;
              }
              msgEl.className = 'alert alert-success';
              msgEl.textContent = `Reading recorded: ${res.reading.reading_m3} M³ → consumption ${res.reading.consumption_m3} M³ → KES ${fmt(res.reading.amount_kes)}`;
              msgEl.classList.remove('hidden');
              document.getElementById('readingInput').value = '';
              setTimeout(() => location.reload(), 1800);
            })
            .catch(() => {
              submitBtn.disabled = false;
              msgEl.className = 'alert alert-error';
              msgEl.textContent = 'Failed to submit reading.';
              msgEl.classList.remove('hidden');
            });
        });
      }
      /* Coordinates toggle */
      const coordToggle = document.getElementById('coordToggle');
      if (coordToggle) {
        coordToggle.addEventListener('click', () => {
          const body = document.getElementById('coordBody');
          const chev = document.getElementById('coordChev');
          body.classList.toggle('hidden');
          chev.textContent = body.classList.contains('hidden') ? '▾' : '▴';
        });
      }
    })
    .catch(() => { loader.textContent = 'Failed to load consumer details.'; });
}

function buildMapsUrl(c) {
  if (c.latitude != null && c.longitude != null) {
    return `https://www.google.com/maps/search/?api=1&query=${c.latitude},${c.longitude}`;
  }
  if (c.address) {
    return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(c.address)}`;
  }
  return null;
}

/* ═══════════════════════════════════════════════
   EDIT MODE
   ═══════════════════════════════════════════════ */

const EDITABLE_FIELDS = [
  { field: 'cust_name',                id: 'custNameValue', type: 'text',   required: true },
  { field: 'acc_name',                 id: 'accName',       type: 'text',   required: true },
  { field: 'meter_acc_no',             id: 'meterAccNo',    type: 'text',   required: true },
  { field: 'contact',                  id: 'contact',       type: 'text',   required: true },
  { field: 'email',                    id: 'email',         type: 'email',  required: false },
  { field: 'address',                  id: 'address',       type: 'text',   required: false },
  { field: 'latitude',                 id: 'latitude',      type: 'number', required: false, step: 'any' },
  { field: 'longitude',                id: 'longitude',     type: 'number', required: false, step: 'any' },
  { field: 'meter_initial_reading_m3', id: 'initialReading',type: 'number', required: true,  step: '0.01', min: 0 },
];

let EDIT_BACKUP = null;

function toggleEditMode() {
  const actions = document.getElementById('editActions');
  const btn = document.getElementById('editToggleBtn');
  const editMsg = document.getElementById('editMsg');

  if (actions.classList.contains('hidden')) {
    // Enter edit
    EDIT_BACKUP = {};
    EDITABLE_FIELDS.forEach(({ field, id }) => {
      const el = document.getElementById(id);
      EDIT_BACKUP[id] = el.innerHTML;
    });

    EDITABLE_FIELDS.forEach(({ field, id, type, required, step, min }) => {
      const el = document.getElementById(id);
      const current = CURRENT_CONSUMER[field];
      const value = current == null ? '' : current;
      el.innerHTML = `<input type="${type}" data-field="${field}"` +
        (required ? ' required' : '') +
        (step ? ` step="${step}"` : '') +
        (min != null ? ` min="${min}"` : '') +
        ` value="${esc(value)}">`;
    });

    actions.classList.remove('hidden');
    btn.innerHTML = '<span>✏️</span> Editing…';
    btn.disabled = true;
    editMsg.classList.add('hidden');
    document.getElementById('editActions').classList.remove('hidden');
  }
}

function exitEditMode(restore = true) {
  const actions = document.getElementById('editActions');
  const btn = document.getElementById('editToggleBtn');
  if (restore && EDIT_BACKUP) {
    Object.entries(EDIT_BACKUP).forEach(([id, html]) => {
      document.getElementById(id).innerHTML = html;
    });
  }
  actions.classList.add('hidden');
  btn.disabled = false;
  btn.innerHTML = '<span>✏️</span> Edit';
  EDIT_BACKUP = null;
  const editMsg = document.getElementById('editMsg');
  editMsg.classList.add('hidden');
}

async function saveConsumerEdits() {
  const msgEl = document.getElementById('editMsg');
  const saveBtn = document.getElementById('saveEditBtn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  const payload = {};
  EDITABLE_FIELDS.forEach(({ field, id }) => {
    const el = document.getElementById(id);
    const input = el.querySelector('input');
    if (!input) return;
    let v = input.value.trim();
    if (field === 'meter_initial_reading_m3') {
      v = v === '' ? 0 : parseFloat(v);
    } else if (field === 'latitude' || field === 'longitude') {
      v = v === '' ? null : parseFloat(v);
    } else if (field === 'email') {
      v = v || null;
    } else if (field === 'address') {
      v = v || null;
    }
    payload[field] = v;
  });

  try {
    const r = await adminFetch(`${API}/api/admin/consumer/${CURRENT_CONSUMER.id}/update`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      msgEl.className = 'alert alert-success';
      msgEl.textContent = '✅ Saved. Reloading…';
      msgEl.classList.remove('hidden');
      setTimeout(() => location.reload(), 700);
    } else {
      msgEl.className = 'alert alert-error';
      msgEl.textContent = data.error || 'Save failed.';
      msgEl.classList.remove('hidden');
      saveBtn.disabled = false;
      saveBtn.textContent = '💾 Save Changes';
    }
  } catch (e) {
    if (e.message && e.message.includes('Redirecting')) return;
    msgEl.className = 'alert alert-error';
    msgEl.textContent = 'Network error.';
    msgEl.classList.remove('hidden');
    saveBtn.disabled = false;
    saveBtn.textContent = '💾 Save Changes';
  }
}

/* Wire edit buttons once the page is ready */
window.addEventListener('DOMContentLoaded', () => {
  const saveBtn = document.getElementById('saveEditBtn');
  const cancelBtn = document.getElementById('cancelEditBtn');
  if (saveBtn) saveBtn.addEventListener('click', saveConsumerEdits);
  if (cancelBtn) cancelBtn.addEventListener('click', () => exitEditMode(true));
});

/* ═══════════════════════════════════════════════
   PAYMENT
   ═══════════════════════════════════════════════ */
async function submitPayment(consumerId) {
  const msgEl = document.getElementById('paymentMsg');
  const btn = document.getElementById('paymentSubmitBtn');

  const amount = parseFloat(document.getElementById('payAmount').value);
  const method = document.getElementById('payMethod').value;
  const reference = document.getElementById('payReference').value.trim();

  if (!amount || amount <= 0) {
    msgEl.className = 'alert alert-error';
    msgEl.textContent = 'Enter a valid payment amount greater than zero.';
    msgEl.classList.remove('hidden');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Recording…';
  msgEl.classList.add('hidden');

  try {
    const r = await adminFetch(`${API}/api/admin/consumer/${consumerId}/payment`, {
      method: 'POST',
      body: JSON.stringify({ amount_kes: amount, method, reference }),
    });
    const data = await r.json();

    if (r.ok && data.ok) {
      const lines = data.allocations.map(a =>
        `• ${a.reading_date} — applied KES ${a.applied} ` +
        `(new balance KES ${a.new_balance})${a.note ? ' ' + a.note : ''}`
      ).join('\n');
      msgEl.className = 'alert alert-success';
      msgEl.textContent = `✅ Payment of KES ${fmt(data.amount_kes)} recorded.\n${lines}`;
      msgEl.style.whiteSpace = 'pre-line';
      msgEl.classList.remove('hidden');
      document.getElementById('paymentForm').reset();

      /* One-shot storage: shown once after the auto-reload, then cleared. */
      sessionStorage.setItem('pendingReceiptPid', data.payment_id);
      sessionStorage.setItem('pendingReceiptNo',  data.receipt_no);
      sessionStorage.setItem('pendingReceiptCid', consumerId);

      setTimeout(() => location.reload(), 2200);
    } else {
      msgEl.className = 'alert alert-error';
      msgEl.textContent = data.error || 'Payment failed.';
      msgEl.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = 'Record Payment';
    }
  } catch (e) {
    if (e.message && e.message.includes('Redirecting')) return;
    msgEl.className = 'alert alert-error';
    msgEl.textContent = 'Network error.';
    msgEl.classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = 'Record Payment';
  }
}

function renderPaymentHistory() {
  const wrap  = document.getElementById('paymentHistory');
  const list  = document.getElementById('paymentHistoryList');
  const body  = document.getElementById('paymentHistoryBody');
  const btn   = document.getElementById('paymentToggleBtn');
  if (!wrap || !list || !body || !btn) return;

  if (!CURRENT_PAYMENTS.length) {
    wrap.classList.add('hidden');
    return;
  }

  // Cap at 10 most recent (backend already returns ≤10, this is belt-and-braces)
  const recent = CURRENT_PAYMENTS.slice(0, 10);

  list.innerHTML = recent.map((p) => `
    <div class="payment-item">
      <div>
        <div class="amount">KES ${fmt(p.amount_kes)}</div>
        <div class="meta">
          ${esc(p.method || '—')}${p.reference ? ' · ' + esc(p.reference) : ''}
          ${p.recorded_by ? ' · by ' + esc(p.recorded_by) : ''}
        </div>
      </div>
      <div class="meta">${fmtDateTime(p.created_at)}</div>
    </div>`).join('');

  // Wire the toggle once per page load
  if (!btn.dataset.wired) {
    btn.addEventListener('click', () => {
      const nowHidden = body.classList.toggle('hidden');
      btn.classList.toggle('open', !nowHidden);
      btn.setAttribute('aria-expanded', String(!nowHidden));
    });
    btn.dataset.wired = '1';
  }

  wrap.classList.remove('hidden');
}

/* ═══════════════════════════════════════════════
   REACTIVATE / DELETE
   ═══════════════════════════════════════════════ */
async function reactivateConsumer(id, name) {
  if (!confirm(`Reactivate ${name}? Readings and reminders will resume.`)) return;
  try {
    const r = await adminFetch(`${API}/api/admin/consumer/${id}/reactivate`, { method: 'POST' });
    const data = await r.json();
    if (r.ok && data.ok) { alert(`✅ ${name} reactivated.`); location.reload(); }
    else { alert(`❌ ${data.error || 'Reactivate failed.'}`); }
  } catch (e) {
    if (e.message && e.message.includes('Redirecting')) return;
    alert('Network error.');
  }
}

async function deleteConsumer(id, name) {
  if (!confirm(`⚠️ PERMANENTLY DELETE ${name}?\n\nErases customer, all readings, payments, and logs.\nThis CANNOT be undone.`)) return;
  if (!confirm(`Final confirmation — delete ${name} forever?`)) return;

  try {
    const r = await adminFetch(`${API}/api/admin/consumer/${id}`, { method: 'DELETE' });
    const data = await r.json();
    if (r.ok && data.ok) { alert(`🗑️ ${name} permanently deleted.`); location.href = '/'; }
    else { alert(`❌ ${data.error || 'Delete failed.'}`); }
  } catch (e) {
    if (e.message && e.message.includes('Redirecting')) return;
    alert('Network error.');
  }
}

/* ═══════════════════════════════════════════════
   NOTIFY BUTTONS
   ═══════════════════════════════════════════════ */
function wireNotifyButtons(consumerId) {
  const smsBtn = document.getElementById('sendSmsBtn');
  const emailBtn = document.getElementById('sendEmailBtn');
  const msgEl = document.getElementById('notifyMsg');

  function send(channel, btn) {
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">⏳</span> Sending…';
    msgEl.classList.add('hidden');

    fetch(`${API}/api/consumer/${consumerId}/notify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel }),
    })
      .then((r) => r.json().then((d) => ({ ok: r.ok, data: d })))
      .then(({ ok, data }) => {
        btn.disabled = false;
        btn.innerHTML = original;
        if (ok && data.ok) {
          msgEl.className = 'alert alert-success';
          msgEl.textContent = `${data.message} → ${data.to}`;
        } else {
          msgEl.className = 'alert alert-error';
          msgEl.textContent = data.error || 'Send failed.';
        }
        msgEl.classList.remove('hidden');
      })
      .catch(() => {
        btn.disabled = false; btn.innerHTML = original;
        msgEl.className = 'alert alert-error';
        msgEl.textContent = 'Network error.';
        msgEl.classList.remove('hidden');
      });
  }

  smsBtn.replaceWith(smsBtn.cloneNode(true));
  emailBtn.replaceWith(emailBtn.cloneNode(true));

  document.getElementById('sendSmsBtn')
    .addEventListener('click', (e) => send('sms', e.currentTarget));
  document.getElementById('sendEmailBtn')
    .addEventListener('click', (e) => send('email', e.currentTarget));
}

/* ═══════════════════════════════════════════════
   BILL PAGE
   ═══════════════════════════════════════════════ */
function initBillPage(readingId) {
  const loader = document.getElementById('billLoader');
  if (!readingId) { loader.textContent = 'No reading selected.'; return; }

  fetch(`${API}/api/reading/${readingId}`)
    .then((r) => r.json())
    .then((data) => {
      loader.classList.add('hidden');
      document.getElementById('billCard').classList.remove('hidden');
      const c = data.consumer, r = data.reading;
      document.getElementById('bCustName').textContent = c.cust_name;
      document.getElementById('bAccName').textContent = c.acc_name;
      document.getElementById('bMeterAcc').textContent = c.meter_acc_no;
      document.getElementById('bContact').textContent = c.contact;
      document.getElementById('bAddress').textContent = c.address || '—';
      document.getElementById('bDate').textContent = fmtDate(r.reading_date);
      document.getElementById('bM3').textContent = Number(r.reading_m3).toFixed(2) + ' M³';
      document.getElementById('bAmount').textContent = 'KES ' + fmt(r.amount_kes);
      document.getElementById('bPaid').textContent = 'KES ' + fmt(r.amount_paid);
      document.getElementById('bBalance').textContent = 'KES ' + fmt(r.balance);
      document.getElementById('billStatusBadge').textContent = r.bill_status;
    })
    .catch(() => { loader.textContent = 'Failed to load bill.'; });
}

/* ═══════════════════════════════════════════════
   RECEIPT DOWNLOAD
   ═══════════════════════════════════════════════ */
function showDownloadReceiptButton(paymentId, receiptNo) {
  const row = document.getElementById('receiptDownloadRow');
  const btn = document.getElementById('downloadReceiptBtn');
  if (!row || !btn) return;

  row.classList.remove('hidden');
  btn.disabled = false;
  btn.dataset.paymentId = paymentId;
  btn.innerHTML = `<span class="btn-icon">📄</span> Download Receipt (PDF)`;

  btn.onclick = async () => {
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">⏳</span> Generating…';

    try {
      const r = await fetch(
        `${API}/api/admin/payment/${paymentId}/receipt.pdf`,
        { credentials: 'same-origin' }
      );

      if (r.status === 401) { requireLoginRedirect(); return; }
      if (!r.ok) {
        let msg = 'Receipt generation failed.';
        try { const j = await r.json(); msg = j.error || msg; } catch {}
        throw new Error(msg);
      }

      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `SimonWater_Receipt_${receiptNo || paymentId}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      btn.innerHTML = '<span class="btn-icon">✓</span> Receipt Already Downloaded';
      btn.classList.add('btn-download-done');
      btn.disabled = true;
    } catch (e) {
      btn.disabled = false;
      btn.innerHTML = `<span class="btn-icon">📄</span> Retry Download Receipt`;
      alert('❌ ' + (e.message || 'Receipt generation failed.'));
    }
  };
}
