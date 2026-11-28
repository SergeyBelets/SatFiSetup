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
      commit: '6079f3e46af93faed50febd31be7f3bef7a56b98',
      script: 'tools/satoshifi-start.sh',
      scriptSha256: '961eb25275f3cd33ea51abb3e4b157497e676beb1ab3771b6c6073421f30ec5a'
    }),
    LINKS: Object.freeze({
      telegram: 'https://t.me/',
      repository: 'https://github.com/SergeyBelets/SatFiSetup',
      whitepaper: 'satoshifi-whitepaper.html'
    })
  })
});
