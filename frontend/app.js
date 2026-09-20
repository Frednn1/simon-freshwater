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

/* ─── Redirect to login on 401 ─── */
async function adminFetch(url, options = {}) {
  options.credentials = 'same-origin';
  options.headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  const r = await fetch(url, options);
  if (r.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = `/admin/login?next=${next}`;
    throw new Error('Redirecting to login…');
  }
  return r;
}

/* ═══════════════════════════════════════════════
   HOME PAGE
   ═══════════════════════════════════════════════ */
function initHomePage() {
  const input = document.getElementById('searchInput');
  const btn = document.getElementById('searchBtn');
  const loader = document.getElementById('searchLoader');
  const errorBox = document.getElementById('searchError');
  const resultsBox = document.getElementById('resultsContainer');
  const resultsList = document.getElementById('resultsList');

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

    fetch(`${API}/api/search?q=${encodeURIComponent(q)}`)
      .then((r) => r.json())
      .then((data) => {
        loader.classList.add('hidden');
        btn.disabled = false;
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
        errorBox.textContent = 'Something went wrong.';
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
    .addEventListener('click', openNewCustomerModal);
  document.getElementById('openTerminateBtn')
    .addEventListener('click', openTerminateModal);

  const termInput = document.getElementById('termSearchInput');
  let termDebounce;
  termInput.addEventListener('input', () => {
    clearTimeout(termDebounce);
    termDebounce = setTimeout(runTerminateSearch, 400);
  });
}

/* ═══════════════════════════════════════════════
   NEW CUSTOMER MODAL
   ═══════════════════════════════════════════════ */
function openNewCustomerModal() {
  document.getElementById('newCustomerModal').classList.remove('hidden');
  document.getElementById('newCustomerMsg').classList.add('hidden');
  document.getElementById('newCustomerForm').reset();
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
      method: 'POST',
      body: JSON.stringify(payload),
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

  fetch(`${API}/api/search?q=${encodeURIComponent(q)}`)
    .then((r) => r.json())
    .then((data) => {
      loader.classList.add('hidden');
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
function initConsumerPage(consumerId) {
  const loader = document.getElementById('pageLoader');
  if (!consumerId) { loader.textContent = 'No consumer selected.'; return; }

  fetch(`${API}/api/consumer/${consumerId}`)
    .then((r) => r.json())
    .then((data) => {
      loader.classList.add('hidden');
      const c = data.consumer;
      const s = data.overall_status;
      const readings = data.readings || [];
      const isActive = c.is_active !== false;

      if (!isActive) {
        document.getElementById('terminatedBanner').classList.remove('hidden');
        document.getElementById('terminatedAtText').textContent =
          c.terminated_at ? `Terminated on ${fmtDateTime(c.terminated_at)}` : '';
        document.getElementById('adminActions').classList.remove('hidden');
        document.getElementById('readingFormCard').classList.add('hidden');
        document.getElementById('notifyCard').classList.add('hidden');

        document.getElementById('reactivateBtn')
          .addEventListener('click', () => reactivateConsumer(c.id, c.cust_name));
        document.getElementById('deleteBtn')
          .addEventListener('click', () => deleteConsumer(c.id, c.cust_name));
      }

      document.getElementById('overviewCard').classList.remove('hidden');
      document.getElementById('custNameHeading').textContent = c.cust_name;
      document.getElementById('accName').textContent = c.acc_name;
      document.getElementById('meterAccNo').textContent = c.meter_acc_no;
      document.getElementById('contact').textContent = c.contact;
      document.getElementById('email').textContent = c.email || '—';

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

      if (isActive) {
        const notifyCard = document.getElementById('notifyCard');
        const outstanding = ['DUE', 'OVERDUE', 'OVERDUE_APPROACHING'].includes(s.status);
        const available = data.available_channels || [];
        if (outstanding && available.length > 0) {
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
  if (!confirm(`⚠️ PERMANENTLY DELETE ${name}?\n\nErases customer, all readings, and logs.\nThis CANNOT be undone.`)) return;
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
