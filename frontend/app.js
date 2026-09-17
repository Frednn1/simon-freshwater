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
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-KE', {
    day: '2-digit', month: 'short', year: 'numeric',
  });
}

function badgeClass(status) {
  const map = {
    CLEARED: 'badge-cleared',
    PREPAYMENT: 'badge-prepaid',
    DUE: 'badge-due',
    OVERDUE: 'badge-overdue',
    OVERDUE_APPROACHING: 'badge-approaching',
  };
  return map[status] || 'badge-none';
}

function badge(status, label) {
  return `<span class="badge ${badgeClass(status)}">${esc(label)}</span>`;
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
          errorBox.textContent =
            'No matching consumer found. Please check your name or account number.';
          errorBox.classList.remove('hidden');
          return;
        }
        resultsList.innerHTML = results
          .map(
            (c) => `
          <a class="result-item" href="/consumer?id=${c.id}">
            <div>
              <div class="result-name">${esc(c.cust_name)}</div>
              <div class="result-acc">Acc: ${esc(c.meter_acc_no)} &middot; ${esc(c.acc_name)}</div>
            </div>
            <div class="result-right">${badge(c.status, c.status_label)}</div>
          </a>`
          )
          .join('');
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
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doSearch();
  });
  input.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(doSearch, 450);
  });
}

/* ═══════════════════════════════════════════════
   CONSUMER PAGE
   ═══════════════════════════════════════════════ */
function initConsumerPage(consumerId) {
  const loader = document.getElementById('pageLoader');
  if (!consumerId) {
    loader.textContent = 'No consumer selected.';
    return;
  }

  fetch(`${API}/api/consumer/${consumerId}`)
    .then((r) => r.json())
    .then((data) => {
      loader.classList.add('hidden');
      const c = data.consumer;
      const s = data.overall_status;
      const readings = data.readings || [];

      /* Overview */
      document.getElementById('overviewCard').classList.remove('hidden');
      document.getElementById('custNameHeading').textContent = c.cust_name;
      document.getElementById('accName').textContent = c.acc_name;
      document.getElementById('meterAccNo').textContent = c.meter_acc_no;
      document.getElementById('contact').textContent = c.contact;
      document.getElementById('email').textContent = c.email || '—';
      document.getElementById('address').textContent = c.address || '—';

      const badgeEl = document.getElementById('overallBadge');
      badgeEl.className = 'badge ' + badgeClass(s.status);
      badgeEl.textContent = s.label;

      /* Status summary */
      const sum = document.getElementById('statusSummary');
      let html = `<p><strong>Overall Status:</strong> ${esc(s.label)}</p>`;
      if (s.total_due > 0)
        html += `<p><strong>Outstanding:</strong> KES ${fmt(s.total_due)}</p>`;
      if (s.total_prepaid > 0)
        html += `<p><strong>Prepaid Credit:</strong> KES ${fmt(s.total_prepaid)}</p>`;
      if (s.last_reading_date)
        html += `<p><strong>Last Reading:</strong> ${fmtDate(s.last_reading_date)} (${s.age_days} days ago)</p>`;
      sum.innerHTML = html;

      /* Readings table */
      document.getElementById('readingsCard').classList.remove('hidden');
      const tbody = document.getElementById('readingsBody');
      tbody.innerHTML = readings
        .map(
          (r) => `
        <tr>
          <td>${fmtDate(r.reading_date)}</td>
          <td>${Number(r.reading_m3).toFixed(2)}</td>
          <td>${fmt(r.amount_kes)}</td>
          <td>${fmt(r.amount_paid)}</td>
          <td>${fmt(r.balance)}</td>
          <td><a href="/bill?id=${r.id}">${esc(r.bill_status)} →</a></td>
        </tr>`
        )
        .join('');

      /* Reading form */
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
            msgEl.textContent =
              `Reading recorded: ${res.reading.reading_m3} M³ → KES ${fmt(res.reading.amount_kes)}`;
            msgEl.classList.remove('hidden');
            document.getElementById('readingInput').value = '';
            setTimeout(() => location.reload(), 1500);
          })
          .catch(() => {
            submitBtn.disabled = false;
            msgEl.className = 'alert alert-error';
            msgEl.textContent = 'Failed to submit reading. Please try again.';
            msgEl.classList.remove('hidden');
          });
      });
    })
    .catch(() => {
      loader.textContent = 'Failed to load consumer details.';
    });
}

/* ═══════════════════════════════════════════════
   BILL PAGE
   ═══════════════════════════════════════════════ */
function initBillPage(readingId) {
  const loader = document.getElementById('billLoader');
  if (!readingId) {
    loader.textContent = 'No reading selected.';
    return;
  }

  fetch(`${API}/api/reading/${readingId}`)
    .then((r) => r.json())
    .then((data) => {
      loader.classList.add('hidden');
      document.getElementById('billCard').classList.remove('hidden');
      const c = data.consumer;
      const r = data.reading;

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
    .catch(() => {
      loader.textContent = 'Failed to load bill.';
    });
}
