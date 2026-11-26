/* SPDX-License-Identifier: Apache-2.0 */
/* custodian.html — client commands rendered from CONFIG.CONFEDERATION.RELEASE only. */
(function () {
  'use strict';

  const R = window.CONFIG.CONFEDERATION.RELEASE;
  const $ = (id) => document.getElementById(id);
  const file = R.script.split('/').pop();
  const RU = document.documentElement.lang === 'ru';
  const T = RU ? { copy: 'Копировать', copied: 'Скопировано', commit: 'Коммит', script: 'Скрипт', sha: 'SHA-256 скрипта', repo: 'Репозиторий' }
               : { copy: 'Copy', copied: 'Copied', commit: 'Commit', script: 'Script', sha: 'Script SHA-256', repo: 'Repository' };

  $('cmd-download').textContent = 'cd ~ && curl -sSfLO ' + R.rawBase + '/' + R.commit + '/' + R.script
    + ' && shasum -a 256 ' + file;
  $('expected-sha').textContent = R.scriptSha256;
  $('cmd-run').textContent = 'bash ' + file + ' --commit ' + R.commit;
  $('cmd-check').textContent = 'bash ~/satfi-sdk/' + R.checkScript;

  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.textContent = T.copy;
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText($(btn.dataset.copy).textContent).then(() => {
        btn.textContent = T.copied;
        setTimeout(() => { btn.textContent = T.copy; }, 1500);
      });
    });
  });

  const repo = document.createElement('a');
  repo.href = R.repository + '/tree/' + R.commit;
  repo.target = '_blank';
  repo.rel = 'noopener';
  repo.textContent = R.repository;
  const dl = $('release');
  [[T.commit, R.commit], [T.script, R.script], [T.sha, R.scriptSha256], [T.repo, repo]].forEach(([k, v]) => {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd');
    if (typeof v === 'string') { dd.textContent = v; dd.className = 'mono'; } else { dd.appendChild(v); }
    dl.append(dt, dd);
  });
})();
