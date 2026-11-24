/* SPDX-License-Identifier: Apache-2.0 */
/* custodian.html — client commands rendered from CONFIG.CONFEDERATION.RELEASE only. */
(function () {
  'use strict';

  const R = window.CONFIG.CONFEDERATION.RELEASE;
  const $ = (id) => document.getElementById(id);
  const file = R.script.split('/').pop();

  $('cmd-download').textContent = 'cd ~ && curl -sSfLO ' + R.rawBase + '/' + R.commit + '/' + R.script
    + ' && shasum -a 256 ' + file;
  $('expected-sha').textContent = R.scriptSha256;
  $('cmd-run').textContent = 'bash ' + file + ' --commit ' + R.commit;
  $('cmd-check').textContent = 'bash ~/satfi-sdk/' + R.checkScript;

  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText($(btn.dataset.copy).textContent).then(() => {
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
      });
    });
  });

  const repo = document.createElement('a');
  repo.href = R.repository + '/tree/' + R.commit;
  repo.target = '_blank';
  repo.rel = 'noopener';
  repo.textContent = R.repository;
  const dl = $('release');
  [['Commit', R.commit], ['Script', R.script], ['Script SHA-256', R.scriptSha256], ['Repository', repo]].forEach(([k, v]) => {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd');
    if (typeof v === 'string') { dd.textContent = v; dd.className = 'mono'; } else { dd.appendChild(v); }
    dl.append(dt, dd);
  });
})();
