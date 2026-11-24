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
    OPERATOR_ADDRESS: '0xF66458BB45059b0E817d9ECE6504Acf322aA94f4',
    MIN_PARTICIPANTS: 1,
    MAX_PARTICIPANTS: 100,
    DASHBOARD_REFRESH_SECONDS: 60,
    SESSION_SIGNIN_SECONDS: 240,
    RELEASE: Object.freeze({
      repository: 'https://github.com/SergeyBelets/SatFiSetup',
      commit: 'e8fea306a5bd899840230845500cae6b0d35db9e',
      script: 'tools/satoshifi-start.sh',
      scriptSha256: '89a16d18c08471d9a04224a937407c79ca51000ba789aa5fcb8ecb72ba45ec2a'
    }),
    LINKS: Object.freeze({
      telegram: 'https://t.me/',
      repository: 'https://github.com/SergeyBelets/SatFiSetup',
      whitepaper: 'satoshifi-whitepaper.html'
    })
  })
});
