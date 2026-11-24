/* SPDX-License-Identifier: Apache-2.0 */
/* BTCyield — shared UI and wallet (window.App). Injected EIP-1193 wallets only; no browser storage. */
(function () {
  'use strict';

  const NET = window.CONFIG.network;

  /* ── Nav scroll effect ── */
  const nav = document.getElementById('nav');
  window.addEventListener('scroll', () => nav.classList.toggle('scrolled', window.scrollY > 50), { passive: true });

  /* ── Mobile menu ── */
  const mobileToggle = document.querySelector('.mobile-toggle');
  const navLinks = document.querySelector('.nav-links');
  const navOverlay = document.getElementById('nav-overlay');
  function closeMenu() {
    mobileToggle.classList.remove('active');
    navLinks.classList.remove('open');
    navOverlay.classList.remove('active');
    document.body.style.overflow = '';
  }
  if (mobileToggle) {
    mobileToggle.addEventListener('click', () => {
      const isOpen = navLinks.classList.toggle('open');
      mobileToggle.classList.toggle('active');
      navOverlay.classList.toggle('active', isOpen);
      document.body.style.overflow = isOpen ? 'hidden' : '';
    });
    navLinks.querySelectorAll('a').forEach((link) => link.addEventListener('click', closeMenu));
    navOverlay.addEventListener('click', closeMenu);
  }

  /* ── Scroll reveal ── */
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry, i) => {
      if (entry.isIntersecting) {
        setTimeout(() => entry.target.classList.add('visible'), i * 60);
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1 });
  document.querySelectorAll('.reveal').forEach((el) => observer.observe(el));

  /* ── Wallet ── */
  const walletBtns = Array.from(document.querySelectorAll('[data-wallet-button]'));
  const modalOverlay = document.getElementById('wallet-modal');
  const modalClose = document.getElementById('wallet-modal-close');
  const listeners = [];
  let account = null;

  const truncate = (addr) => addr.slice(0, 6) + '…' + addr.slice(-4);

  function setAccount(address) {
    const next = address ? ethers.utils.getAddress(address) : null;
    if (next === account) return;
    account = next;
    walletBtns.forEach((btn) => {
      btn.classList.toggle('connected', !!account);
      const label = btn.querySelector('.wallet-label') || btn;
      label.textContent = account ? truncate(account) : 'Connect Wallet';
    });
    listeners.forEach((cb) => cb(account));
  }

  function openModal() {
    if (account) { setAccount(null); return; }
    modalOverlay.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    modalOverlay.classList.remove('active');
    document.body.style.overflow = '';
  }

  async function connect() {
    closeModal();
    if (typeof window.ethereum === 'undefined') {
      window.open(window.CONFIG.walletDownloadUrl, '_blank', 'noopener');
      return;
    }
    const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    const chainId = await window.ethereum.request({ method: 'eth_chainId' });
    if (chainId.toLowerCase() !== NET.chainIdHex) {
      await window.ethereum.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: NET.chainIdHex }] });
    }
    setAccount(accounts[0]);
  }

  walletBtns.forEach((btn) => btn.addEventListener('click', openModal));
  if (modalClose) modalClose.addEventListener('click', closeModal);
  if (modalOverlay) modalOverlay.addEventListener('click', (e) => { if (e.target === modalOverlay) closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
  document.querySelectorAll('.wallet-option').forEach((opt) => {
    opt.setAttribute('role', 'button');
    opt.setAttribute('tabindex', '0');
    opt.addEventListener('click', () => connect().catch((err) => listeners.forEach((cb) => cb(null, err))));
    opt.addEventListener('keydown', (e) => { if (e.key === 'Enter') opt.click(); });
  });

  /* Restore an already authorized wallet on the configured network (the wallet keeps it, not the page). */
  if (typeof window.ethereum !== 'undefined') {
    Promise.all([
      window.ethereum.request({ method: 'eth_accounts' }),
      window.ethereum.request({ method: 'eth_chainId' })
    ]).then(([accounts, chainId]) => {
      if (accounts.length > 0 && chainId.toLowerCase() === NET.chainIdHex) setAccount(accounts[0]);
    });
    window.ethereum.on('accountsChanged', (accounts) => setAccount(accounts.length ? accounts[0] : null));
    window.ethereum.on('chainChanged', () => window.location.reload());
  }

  window.App = Object.freeze({
    account: () => account,
    truncate,
    connect: openModal,
    onWallet: (cb) => { listeners.push(cb); cb(account); },
    signer: () => {
      if (!account) throw new Error('Connect a wallet on ' + NET.name + ' first.');
      return new ethers.providers.Web3Provider(window.ethereum).getSigner();
    },
    provider: () => new ethers.providers.Web3Provider(window.ethereum)
  });
})();
