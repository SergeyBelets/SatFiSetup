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
      commit: '33d4cfc17570cedbdcc4b7b7497d22d7317ddea6',
      script: 'tools/satoshifi-start.sh',
      scriptSha256: 'cb66d61e697aaa3a80871df135620d45f1bf0f99f33f03aa1e382c44c0e52e50'
    }),
    LINKS: Object.freeze({
      telegram: 'https://t.me/',
      repository: 'https://github.com/SergeyBelets/SatFiSetup',
      whitepaper: 'satoshifi-whitepaper.html'
    })
  })
});
