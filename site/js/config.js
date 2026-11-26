/* SPDX-License-Identifier: Apache-2.0 */
/* BTCyield — single configuration entry point. All addresses, URLs and constants live here only. */
window.CONFIG = Object.freeze({
  network: Object.freeze({
    name: 'Ethereum',
    chainId: 1,
    chainIdHex: '0x1',
    explorerUrl: 'https://etherscan.io'
  }),
  walletDownloadUrl: 'https://metamask.io/download/',
  CONFEDERATION: Object.freeze({
    API_BASE: 'https://api.btcyield.online/v1',
    SITE_ORIGIN: 'https://btcyield.online',
    SIWE_DOMAIN: 'btcyield.online',
    PAGE_PATH: '/confederation.html',
    OPERATOR_EMAIL: 'sb@unilayer.solutions',
    OPERATOR_TELEGRAM: 'sergebelets',
    OPERATOR_ADDRESS: '0x46CbF0964BCE33B818BC08e9224bEC1e99e9496A',
    MIN_PARTICIPANTS: 1,
    MAX_PARTICIPANTS: 100,
    DASHBOARD_REFRESH_SECONDS: 60,
    SESSION_SIGNIN_SECONDS: 240,
    RELEASE: Object.freeze({
      repository: 'https://github.com/SergeyBelets/SatFiSetup',
      rawBase: 'https://raw.githubusercontent.com/SergeyBelets/SatFiSetup',
      checkScript: 'tools/satoshifi-check.sh',
      commit: 'ad5d5054de9c7b825573db29c8866da7edf678f1',
      script: 'tools/satoshifi-start.sh',
      scriptSha256: '2b4031a9b55426b79cc284a44b4df5c7f6dc93df71cb971e87999d8f69f5a5fc'
    }),
    LINKS: Object.freeze({
      telegram: 'https://t.me/',
      repository: 'https://github.com/SergeyBelets/SatFiSetup',
      whitepaper: 'satoshifi-whitepaper.html'
    })
  })
});
