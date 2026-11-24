/* SPDX-License-Identifier: Apache-2.0 */
/* confederation.html — five contexts (pitch, contact/request, ceremony, confederation, both)
   plus the ?bind= mode. All state lives in this closure; no browser storage. */
(function () {
  'use strict';

  const C = window.CONFIG.CONFEDERATION;
  const NET = window.CONFIG.network;
  const F = window.SatoshiFiFormats;
  const $ = (id) => document.getElementById(id);
  const PAGE_URI = C.SITE_ORIGIN + C.PAGE_PATH;
  const ZK_ABI = ['function getMainchainHeight() view returns (uint256)', 'function isFresh() view returns (bool)'];

  const state = { session: null, me: null, items: [], current: null, view: null, timer: null };

  /* ── helpers ── */
  function show(id) {
    ['ctx-intro', 'ctx-signin', 'ctx-contact', 'ctx-dash', 'ctx-bind'].forEach((s) => { $(s).hidden = s !== id; });
  }

  function pageError(err) {
    const el = $('page-error');
    if (!err) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.textContent = err.message || String(err);
  }

  function setStatus(el, text, kind) {
    el.className = 'status' + (kind ? ' ' + kind : '');
    el.textContent = text;
  }

  function txLink(el, prefix, hash) {
    setStatus(el, prefix + ' ', 'ok');
    const a = document.createElement('a');
    a.href = NET.explorerUrl + '/tx/' + hash;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = hash;
    el.appendChild(a);
  }

  function kv(dl, pairs) {
    dl.textContent = '';
    pairs.forEach(([label, value]) => {
      const dt = document.createElement('dt');
      const dd = document.createElement('dd');
      dt.textContent = label;
      if (value instanceof Node) dd.appendChild(value); else dd.textContent = String(value);
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
  }

  function rows(tbody, data, highlight) {
    const labels = Array.from(tbody.closest('table').querySelectorAll('th')).map((th) => th.textContent);
    tbody.textContent = '';
    data.forEach((cells, r) => {
      const tr = document.createElement('tr');
      if (highlight && highlight[r]) tr.className = 'me';
      cells.forEach((cell, i) => {
        const td = document.createElement('td');
        td.setAttribute('data-label', labels[i]);
        td.textContent = cell === null || cell === undefined ? '—' : String(cell);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  function age(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 90) return seconds + ' s ago';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min ago';
    return Math.round(seconds / 3600) + ' h ago';
  }

  function explorerAddress(address) {
    const a = document.createElement('a');
    a.href = NET.explorerUrl + '/address/' + address;
    a.target = '_blank';
    a.rel = 'noopener';
    a.className = 'mono';
    a.textContent = address;
    return a;
  }

  function copyButton(value) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-ghost btn-small';
    b.textContent = 'Copy';
    b.addEventListener('click', () => navigator.clipboard.writeText(value).then(() => { b.textContent = 'Copied'; }));
    return b;
  }

  async function api(method, path, body, auth) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (auth) headers.Authorization = 'Bearer ' + state.session.token;
    const response = await fetch(C.API_BASE + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body)
    });
    const data = await response.json();
    if (data.status !== 'OK') {
      if (data.code === 'FORBIDDEN' && auth) state.session = null;
      throw new Error((data.code || 'HTTP ' + response.status) + (data.message ? ': ' + data.message : ''));
    }
    return data;
  }

  /* ── contacts block (contexts 1 and 2) ── */
  function renderContacts() {
    document.querySelectorAll('[data-contacts]').forEach((box) => {
      box.textContent = '';
      const dl = document.createElement('dl');
      dl.className = 'kv';
      const mail = document.createElement('a');
      mail.href = 'mailto:' + C.OPERATOR_EMAIL;
      mail.textContent = C.OPERATOR_EMAIL;
      const tg = document.createElement('a');
      tg.href = C.LINKS.telegram + C.OPERATOR_TELEGRAM;
      tg.target = '_blank';
      tg.rel = 'noopener';
      tg.textContent = '@' + C.OPERATOR_TELEGRAM;
      const eth = document.createElement('span');
      eth.appendChild(explorerAddress(C.OPERATOR_ADDRESS));
      eth.appendChild(document.createTextNode(' '));
      eth.appendChild(copyButton(C.OPERATOR_ADDRESS));
      kv(dl, [['E-mail', mail], ['Telegram', tg], ['Operator wallet', eth]]);
      box.appendChild(dl);
      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = 'You can also send a message to the operator wallet from your own wallet: a 0 ETH transaction with your text in the data field. It is public forever.';
      box.appendChild(hint);
    });
  }

  /* ── sign-in (SIWE) ── */
  async function signIn() {
    const statusEl = $('signin-status');
    try {
      const address = App.account();
      setStatus(statusEl, 'Confirm the sign-in message in your wallet…');
      const { nonce } = await api('POST', '/session/nonce', {});
      const now = Math.floor(Date.now() / 1000);
      const message = F.siweMessage({
        domain: C.SIWE_DOMAIN, address, statement: F.loginStatement(), uri: PAGE_URI, chainId: NET.chainId,
        nonce, issuedAt: now, expiration: now + C.SESSION_SIGNIN_SECONDS, resources: []
      });
      const signature = await App.signer().signMessage(message);
      const data = await api('POST', '/session', { message, signature });
      state.session = { token: data.token, address: data.address, role: data.role };
      setStatus(statusEl, '');
      await loadMe();
    } catch (err) {
      setStatus(statusEl, err.message || String(err), 'error');
    }
  }

  /* ── context selection ── */
  async function loadMe() {
    const me = await api('GET', '/me', undefined, true);
    state.me = me;
    state.items = me.ceremonies.map((id) => ({ id, kind: 'CEREMONY' }))
      .concat(me.confederations.map((id) => ({ id, kind: 'CONFEDERATION' })));
    if (!state.items.length) {
      renderContact();
      show('ctx-contact');
      return;
    }
    show('ctx-dash');
    await renderDashboard();
    clearInterval(state.timer);
    state.timer = setInterval(() => renderDashboard().catch(pageError), C.DASHBOARD_REFRESH_SECONDS * 1000);
  }

  function renderSwitcher() {
    const kinds = new Set(state.items.map((i) => i.kind));
    $('dash-tabs').hidden = kinds.size < 2;
    if (!state.view || !kinds.has(state.view)) state.view = kinds.has('CEREMONY') ? 'CEREMONY' : 'CONFEDERATION';
    const ids = state.items.filter((i) => i.kind === state.view).map((i) => i.id);
    const select = $('dash-deployment');
    select.textContent = '';
    ids.forEach((id) => {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = id;
      select.appendChild(opt);
    });
    select.hidden = ids.length < 2;
    if (!ids.includes(state.current)) state.current = ids[0];
    select.value = state.current;
    $('dash-tabs').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.view === state.view));
    $('view-ceremony').hidden = state.view !== 'CEREMONY';
    $('view-confederation').hidden = state.view !== 'CONFEDERATION';
  }

  /* ── ceremony and confederation dashboards ── */
  async function renderDashboard() {
    renderSwitcher();
    const data = await api('GET', '/dashboard?deployment=' + encodeURIComponent(state.current), undefined, true);
    const view = state.view;
    $('dash-title').textContent = (view === 'CEREMONY' ? 'Ceremony ' : 'Confederation ') + data.deployment;
    $('dash-updated').textContent = 'Updated ' + new Date().toLocaleTimeString()
      + (data.snapshot_ts ? ' · operator snapshot ' + age(Math.floor(Date.now() / 1000) - data.snapshot_ts) : ' · no operator snapshot yet');
    pageError(null);
    if (view === 'CEREMONY') renderCeremony(data); else await renderConfederation(data);
  }

  function formatSnapshotValue(v) {
    if (v === null || v === undefined) return '—';
    if (Array.isArray(v)) return v.length ? v.map(formatSnapshotValue).join(', ') : '—';
    if (typeof v === 'object') return Object.keys(v).map((k) => k.replace(/_/g, ' ') + ' ' + formatSnapshotValue(v[k])).join(' · ');
    return String(v);
  }

  function snapshotPairs(snapshot) {
    const pairs = [];
    Object.keys(snapshot).filter((k) => k !== 'manifest').forEach((k) => {
      const v = snapshot[k];
      pairs.push([k.replace(/_/g, ' '), formatSnapshotValue(v)]);
    });
    return pairs.length ? pairs : [['State', 'The operator has not published a snapshot yet.']];
  }

  function renderCeremony(data) {
    $('cer-step').textContent = data.my_step || 'You follow this ceremony as the operator.';
    kv($('cer-state'), snapshotPairs(data.snapshot));
    rows($('cer-members'), data.members.map((m) => [
      m.label, m.personal, m.work, m.bound ? 'yes' : 'no', m.silent ? 'SILENT' : m.verdict, m.stage, age(m.report_age_s)
    ]), data.members.map((m) => m.me));
    renderEvents($('cer-events'), data.events);
    const R = C.RELEASE;
    const command = 'cd ~ && curl -sSfLO https://raw.githubusercontent.com/SergeyBelets/SatFiSetup/' + R.commit + '/' + R.script
      + '\nshasum -a 256 satoshifi-start.sh\nbash satoshifi-start.sh --commit ' + R.commit;
    $('cer-command').textContent = command;
    $('cer-copy').onclick = () => navigator.clipboard.writeText(command).then(() => { $('cer-copy').textContent = 'Copied'; });
    const repo = document.createElement('a');
    repo.href = R.repository;
    repo.target = '_blank';
    repo.rel = 'noopener';
    repo.textContent = R.repository;
    kv($('cer-release'), [['Commit', R.commit], ['Script SHA-256', R.scriptSha256], ['Operator', C.OPERATOR_ADDRESS],
      ['Deployment', data.deployment], ['Repository', repo]]);
  }

  function renderEvents(list, events) {
    list.textContent = '';
    if (!events.length) {
      const li = document.createElement('li');
      li.textContent = 'No events yet.';
      list.appendChild(li);
      return;
    }
    events.forEach((e) => {
      const li = document.createElement('li');
      const time = document.createElement('span');
      time.className = 'event-time';
      time.textContent = new Date(e.at * 1000).toLocaleString();
      li.appendChild(time);
      li.appendChild(document.createTextNode(' ' + e.text));
      list.appendChild(li);
    });
  }

  async function renderConfederation(data) {
    const manifest = (data.snapshot && data.snapshot.manifest) || {};
    const names = Object.keys(manifest);
    kv($('conf-addresses'), names.length ? names.map((k) => [k.replace(/_/g, ' '), explorerAddress(manifest[k])])
      : [['Addresses', 'The operator has not published the confederation manifest yet.']]);
    rows($('conf-members'), data.members.map((m) => [m.label, m.personal, m.work, m.silent ? 'SILENT' : m.verdict,
      age(m.report_age_s)]), data.members.map((m) => m.me));
    renderEvents($('conf-events'), data.events);
    if (!names.length) { kv($('conf-zk'), [['ZK state', '—']]); return; }
    const provider = App.provider();
    const missing = [];
    for (const k of names) {
      if (!ethers.utils.isAddress(manifest[k]) || (await provider.getCode(manifest[k])) === '0x') missing.push(k);
    }
    setStatus($('conf-verify'), missing.length ? 'No contract code at: ' + missing.join(', ') + '. Do not use these addresses.'
      : 'All ' + names.length + ' addresses have contract code on ' + NET.name + '.', missing.length ? 'error' : 'ok');
    if (manifest.zk_state && !missing.includes('zk_state')) {
      const zk = new ethers.Contract(manifest.zk_state, ZK_ABI, provider);
      const [height, fresh] = await Promise.all([zk.getMainchainHeight(), zk.isFresh()]);
      kv($('conf-zk'), [['Accepted Bitcoin height', height.toString()], ['State', fresh ? 'FRESH — deposits accepted' : 'STALE — catching up, deposits wait']]);
    } else {
      kv($('conf-zk'), [['ZK state', 'not in the manifest']]);
    }
  }

  /* ── context 2: request form ── */
  function addParticipantRow(address, label) {
    const row = document.createElement('div');
    row.className = 'participant-row';
    const fields = [['0x… personal address', 'rq-address', address || ''], ['Label (name or organization)', 'rq-label', label || ''],
      ['Contact (optional)', 'rq-contact', '']];
    fields.forEach(([placeholder, cls, value]) => {
      const input = document.createElement('input');
      input.type = 'text';
      input.className = cls;
      input.placeholder = placeholder;
      input.setAttribute('aria-label', placeholder);
      input.value = value;
      row.appendChild(input);
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn-ghost';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => row.remove());
    row.appendChild(remove);
    $('rq-participants').appendChild(row);
  }

  function renderContact() {
    $('rq-operator').value = C.OPERATOR_ADDRESS;
    if (!$('rq-participants').children.length) addParticipantRow(App.account(), '');
    const open = state.me.requests;
    $('my-requests-panel').hidden = !open.length;
    rows($('my-requests'), open.map((r) => [r.request_id, new Date(r.created_at * 1000).toLocaleDateString(), r.participants, r.status]));
  }

  function readRequest() {
    const opt = (v) => (v.trim() ? v.trim() : null);
    const name = $('rq-name').value.trim();
    const email = $('rq-email').value.trim();
    if (!name) throw new Error('Your name is required.');
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw new Error('Enter a valid e-mail.');
    const seen = new Set([C.OPERATOR_ADDRESS]);
    const participants = Array.from($('rq-participants').children).map((row, i) => {
      const raw = row.querySelector('.rq-address').value.trim();
      if (!ethers.utils.isAddress(raw)) throw new Error('Custodian ' + (i + 1) + ': invalid address.');
      const address = ethers.utils.getAddress(raw);
      if (seen.has(address)) throw new Error('Custodian ' + (i + 1) + ': duplicate or the operator address.');
      seen.add(address);
      const label = row.querySelector('.rq-label').value.trim();
      if (!label) throw new Error('Custodian ' + (i + 1) + ': label is required.');
      return { address, label, contact: opt(row.querySelector('.rq-contact').value) };
    });
    if (participants.length < C.MIN_PARTICIPANTS || participants.length > C.MAX_PARTICIPANTS) {
      throw new Error('Custodians: from ' + C.MIN_PARTICIPANTS + ' to ' + C.MAX_PARTICIPANTS + '.');
    }
    return { initiator_address: App.account(), name, email, telegram: opt($('rq-telegram').value),
      organization: opt($('rq-org').value), participants };
  }

  async function submitRequest(event) {
    event.preventDefault();
    const statusEl = $('rq-status');
    try {
      const body = readRequest();
      const issued = Math.floor(Date.now() / 1000);
      setStatus(statusEl, 'Confirm the signature in your wallet…');
      const signature = await App.signer().signMessage(F.requestMessage(C.SIWE_DOMAIN, NET.chainId, issued, body));
      const data = await api('POST', '/requests', { body, issued_at: issued, signature }, true);
      setStatus(statusEl, data.duplicate ? 'This request was already sent.' : 'Sent.', 'ok');
      $('rq-id').textContent = data.request_id;
      const lines = body.participants.map((p) => p.address + ' ' + p.label).join('\n');
      $('rq-mailto').href = 'mailto:' + C.OPERATOR_EMAIL + '?subject=' + encodeURIComponent('SatoshiFi confederation request ' + data.request_id)
        + '&body=' + encodeURIComponent('Request ' + data.request_id + '\n\n' + lines);
      $('rq-onchain').onclick = () => messageOperator(data.request_id, body.participants);
      $('rq-result').hidden = false;
    } catch (err) {
      setStatus(statusEl, err.message || String(err), 'error');
    }
  }

  async function messageOperator(requestId, participants) {
    const statusEl = $('rq-onchain-status');
    try {
      const text = 'SatoshiFi confederation request ' + requestId + '\n' + participants.map((p) => p.address).join('\n');
      setStatus(statusEl, 'Confirm the transaction in your wallet…');
      const hash = await window.ethereum.request({ method: 'eth_sendTransaction', params: [{
        from: App.account(), to: C.OPERATOR_ADDRESS, value: '0x0', chainId: NET.chainIdHex,
        data: ethers.utils.hexlify(ethers.utils.toUtf8Bytes(text))
      }] });
      txLink(statusEl, 'Sent:', hash);
    } catch (err) {
      setStatus(statusEl, err.message || String(err), 'error');
    }
  }

  /* ── binding mode ── */
  function bindParams(query) {
    const p = {
      code: query.get('bind'), deployment: query.get('deployment'), work: query.get('work'),
      wsig: query.get('wsig'), exp: Number(query.get('exp'))
    };
    if (!/^[0-9a-f]{64}$/.test(p.code || '') || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(p.deployment || '')
      || !/^0x[0-9a-fA-F]{130}$/.test(p.wsig || '') || !Number.isSafeInteger(p.exp)) {
      throw new Error('The binding link is incomplete. Run the binding step of satoshifi-start again.');
    }
    F.requireChecksum(p.work, 'work address');
    if (p.exp <= Math.floor(Date.now() / 1000)) throw new Error('The binding link expired. Run the binding step of satoshifi-start again.');
    const wsigText = F.wsigMessage(p.deployment, NET.chainId, p.work, p.code, p.exp);
    if (ethers.utils.verifyMessage(wsigText, p.wsig) !== p.work) {
      throw new Error('The work-key signature does not match the work address. Run the binding step again.');
    }
    return p;
  }

  function startBind(p) {
    show('ctx-bind');
    kv($('bind-info'), [['Deployment', p.deployment], ['Work address', p.work],
      ['Link valid until', new Date(p.exp * 1000).toLocaleString()]]);
    $('bind-sign').addEventListener('click', async () => {
      const statusEl = $('bind-status');
      try {
        if (!App.account()) { App.connect(); return; }
        const personal = App.account();
        const message = F.siweMessage({
          domain: C.SIWE_DOMAIN, address: personal, statement: F.bindingStatement(p.deployment, p.work), uri: PAGE_URI,
          chainId: NET.chainId, nonce: p.code, issuedAt: Math.floor(Date.now() / 1000), expiration: p.exp,
          resources: [F.wsigResource(p.wsig)]
        });
        setStatus(statusEl, 'Confirm the message in your personal wallet…');
        const signature = await App.signer().signMessage(message);
        const data = await api('POST', '/bindings', { deployment: p.deployment, work_address: p.work, code: p.code, exp: p.exp,
          wsig: p.wsig, siwe_message: message, siwe_signature: signature });
        setStatus(statusEl, 'Binding accepted for ' + data.label + '. Return to the terminal.', 'ok');
      } catch (err) {
        const advice = { NOT_ALLOWLISTED: ' This personal address is not in the operator list — contact the operator.',
          CODE_USED: ' Run the binding step of satoshifi-start again.', CODE_EXPIRED: ' Run the binding step again.',
          ALREADY_BOUND: ' This address is already bound — contact the operator.' };
        const code = (err.message || '').split(':')[0];
        setStatus(statusEl, (err.message || String(err)) + (advice[code] || ''), 'error');
      }
    });
  }

  /* ── wiring ── */
  renderContacts();
  $('signin').addEventListener('click', signIn);
  $('rq-add').addEventListener('click', () => addParticipantRow('', ''));
  $('request-form').addEventListener('submit', submitRequest);
  $('dash-deployment').addEventListener('change', (e) => { state.current = e.target.value; renderDashboard().catch(pageError); });
  $('dash-tabs').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    state.view = b.dataset.view;
    renderDashboard().catch(pageError);
  }));

  const query = new URLSearchParams(window.location.search);
  if (query.has('bind')) {
    try { startBind(bindParams(query)); } catch (err) { show('ctx-bind'); setStatus($('bind-status'), err.message, 'error'); $('bind-sign').hidden = true; }
    return;
  }

  App.onWallet((account, err) => {
    if (err) pageError(err);
    clearInterval(state.timer);
    if (!account) {
      state.session = null;
      show('ctx-intro');
      return;
    }
    if (state.session && state.session.address === account) return;
    state.session = null;
    $('signin-address').textContent = account;
    show('ctx-signin');
  });
})();
