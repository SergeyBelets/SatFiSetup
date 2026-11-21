// js/config.js
const CONFIG = {
    // Ethereum Networks
    NETWORKS: {
        SEPOLIA: {
            chainId: 11155111,
            name: 'Sepolia Testnet',
            // Set globalThis.SATOSHIFY_SEPOLIA_RPC_URL before loading this legacy config.
            rpc: globalThis.SATOSHIFY_SEPOLIA_RPC_URL || ''
        }
    },

    // Smart Contract Addresses (updated with V2 system)
    CONTRACTS: {

        // Mining Pool System V2 (new)
        FACTORY: '0x592153a6550515D4775a4c2866909e8cd1519898',
        FROST_SESSION_READER: '0x2eF0a6CD071482dC837667226843884c7B308428',
        GOVERNANCE_INTEGRATOR: '0x22dA77fd875E32dF5C642dbF4654e4Ee232E6519',
        CORE_IMPL_V2: '0x7dB8c8eC91DAb89d9D3A9A809b8C297C7bdbEDa1',
        REWARDS_IMPL_V2: '0xF77bC237071dbad8587070AC3469d0bEb841812A',
        EXTENSIONS_IMPL_V2: '0x53100b81654f9cc0ADF31635FEda268EB0eba2fa',
        REDEMPTION_IMPL_V2: '0x060c06a539030f49B441Ba34E8d6b148e39AF0C5',

        // FROST System
        FROST_COORDINATOR: '0x0849ACD80a2A5c454C17433C895C3f3FD2aF4Ea6',
        INITIAL_FROST: '0xb09c536e4b064E0235b480fe13f54f38e0CE6Fb8',
        FROST_VERIFIER: '0x4dccA4E12d0Cb7B3d713B717f3C4fB348571a4C0',

        // DAO and Bridge
        MULTI_POOL_DAO: '0xc79f572415Dad10b302C1c4611c97Cec8AB767CC',
        BRIDGE_INBOX: '0xB84d53Bae116fDEd1d35D71156489d6F5a6cf336',
        BRIDGE_OUTBOX: '0xb7F782CAB5242f7C7C97FC1361e381c48Cc19941',

        // SPV and Oracle
        SPV_CONTRACT: '0xAA851eFa4A7133C293C8f213651185388377a1E7',
        ORACLE_REGISTRY: '0x5C5b885b5824970813138968de8fbf7c9b07D294',
        ORACLE_VALIDATOR: '0x0cC52c44A416c2dDda089641382664E9870063Ff',
        ORACLE_AGGREGATOR: '0x90f16CDDAf910375c6f2b411e12D80c027e43263',

        // Calculators
        CALCULATOR_REGISTRY: '0xbe7766717f9aC3f1293244E8b4101F7A80596F0a',
        PPLNS_CALCULATOR: '0xfcCf32634b6D3d7534C1A124826AD428506B4918',
        PPS_CALCULATOR: '0xB362942d85bBA91289ed4c9f6c0878e62F5abcb6',
        FPPS_CALCULATOR: '0xEeF773eC7aFc8318EcCBc9cAc6cA1c906Afa80d1',
        SCORE_CALCULATOR: '0x919746B5f49B817498Abe8d191535b340925e1D6',

        // Tokens
        SBTC: '0x443CF36d07032dD2785a548a20f358Db89b36E1D',
        SBCH: '0x4da42a88B8B0C823Dc745fE2B5EAE345380486bc',
        SLTC: '0x1D2398740D562C77809529aF248824fF41B67F45',
        POOL_TOKEN_FACTORY: '0xc189265999dDeeF4c7eAc46A4a41657A17B3EF4D',

        // SBT/NFT Contracts
        MEMBERSHIP_SBT: '0xF42F943746A2ba3E5b813bb2A4187417bCF56BE5',
        ROLE_BADGE_SBT: '0xe22AB5C2c3bC33b3974D91B06C5405e1Ed04eb06'
    },

    // User Roles
    USER_ROLES: {
        USER: {
            id: 'user',
            name: 'User',
            description: 'Pool Creator',
            permissions: ['create_pool', 'manage_pool', 'invite_participants', 'manage_frost'],
            primaryColor: '#2563eb',
            dashboardWidgets: ['pool-creation', 'pool-overview', 'frost-sessions', 'analytics'],
            sidebarSections: ['dkgManagement', 'poolCreation', 'poolManagement', 'nftCollection', 'settings']
        },
        POOL_OWNER: {
            id: 'pool-owner',
            name: 'Pool Owner',
            description: 'Pool owner is managing existing pools',
            permissions: ['manage_pool', 'view_analytics', 'manage_payouts'],
            primaryColor: '#059669',
            dashboardWidgets: ['pool-management', 'mining-stats', 'payouts', 'participants'],
            sidebarSections: ['dkgManagement', 'poolManagement', 'miningDashboard', 'nftCollection', 'settings']
        },
        MINER: {
            id: 'miner',
            name: 'Miner',
            description: 'Owner of workers',
            permissions: ['mine', 'view_stats', 'claim_rewards'],
            primaryColor: '#d97706',
            dashboardWidgets: ['mining', 'rewards', 'pool-stats', 'hardware'],
            sidebarSections: ['dkgManagement', 'miningDashboard', 'nftCollection', 'settings']
        },
        CUSTODIAL: {
            id: 'custodial',
            name: 'Custodial',
            description: 'FROST signature manager',
            permissions: ['manage_frost', 'sign_transactions', 'manage_custody'],
            primaryColor: '#9333ea',
            dashboardWidgets: ['custody', 'frost-signatures', 'security', 'keys'],
            sidebarSections: ['dkgManagement', 'poolManagement', 'nftCollection', 'settings']
        },
        INITIATOR: {
            id: 'initiator',
            name: 'Initiator',
            description: 'DKG sessions initiator',
            permissions: ['create_dkg', 'invite_participants', 'manage_sessions'],
            primaryColor: '#0891b2',
            dashboardWidgets: ['dkg-sessions', 'participants', 'invitations', 'sessions'],
            sidebarSections: ['dkgManagement', 'poolCreation', 'poolManagement', 'nftCollection', 'settings']
        }
    },

    // App Settings
    APP_SETTINGS: {
        DEFAULT_NETWORK: 'SEPOLIA',
        POLLING_INTERVAL: 15000,
        TRANSACTION_TIMEOUT: 300000,
        NOTIFICATION_TIMEOUT: 5000,
        MAX_PARTICIPANTS: 100,
        MIN_THRESHOLD: 1,
        SESSION_TIMEOUT: 86400
    },

    // UI Configuration
    UI_CONFIG: {
        THEME: 'classic',
        ANIMATIONS_ENABLED: true,
        AUTO_REFRESH: true,
        COMPACT_MODE: false,
        DEVELOPER_MODE: false
    },

    // ИСПРАВЛЕННЫЕ ABI
    ABI: {
        FACTORY: [

            "function createPool(tuple(string asset, string poolId, uint256 pubX, uint256 pubY, string mpName, string mpSymbol, bool restrictedMp, bytes32 create2Salt, bytes payoutScript, uint256 calculatorId)) external returns (address, address)",
            "function createPoolFromFrost(uint256 sessionId, string calldata asset, string calldata poolId, string calldata mpName, string calldata mpSymbol, bool restrictedMp, bytes32 create2Salt, bytes calldata payoutScript, uint256 calculatorId) external returns (address, address)",
            "function hasRole(bytes32 role, address account) external view returns (bool)",
            "function grantRole(bytes32 role, address account) external",
            "function setDependencies(address _poolImplementation, address _spvContract, address _frostCoordinator, address _calculatorRegistry, address _stratumDataAggregator, address _stratumDataValidator, address _oracleRegistry, address _poolTokenFactory, address _multiPoolDAO) external",
            "function setOptionalDependencies(address _policyTemplate, address _membershipSBT, address _roleBadgeSBT) external",
            "function poolImplementation() external view returns (address)",
            "function spvContract() external view returns (address)",
            "function frostCoordinator() external view returns (address)",
            "function calculatorRegistry() external view returns (address)",
            "function stratumDataAggregator() external view returns (address)",
            "function stratumDataValidator() external view returns (address)",
            "function oracleRegistry() external view returns (address)",
            "function poolTokenFactory() external view returns (address)",
            "function multiPoolDAO() external view returns (address)",
            "function membershipSBT() external view returns (address)",
            "function roleBadgeSBT() external view returns (address)",
            "function getAllPools() external view returns (address[])",
            "function getPoolCount() external view returns (uint256)",


            "function deactivatePool(address pool, string calldata reason) external",
            "function reactivatePool(address pool) external",
            "function getPoolInfo(address pool) external view returns (tuple(address poolAddress, address mpToken, string poolId, bool isActive, uint256 createdAt, address creator))",
            "function getActivePools() external view returns (address[])",
            "function ADMIN_ROLE() external pure returns (bytes32)",
            "function POOL_MANAGER_ROLE() external pure returns (bytes32)",


            "event PoolCreated(address indexed poolCore, address indexed poolRewards, address indexed poolExtensions, address poolRedemption, address mpToken, string asset, string poolId, string mpName, string mpSymbol, bool restrictedMp, uint256 pubX, uint256 pubY, bytes payoutScript, uint256 calculatorId)",
            "event PoolDeactivated(address indexed pool, string reason)",
            "event PoolReactivated(address indexed pool)"
        ],

        INITIAL_FROST: [
            "function createSession(bytes32 pubKeyX, bytes32 pubKeyY, address[] calldata initialParticipants, uint32 threshold, uint256 deadline, uint8 purpose) external returns (uint256)",
            "function getSession(uint256 sessionId) external view returns (uint256 id, address creator, bytes memory groupPubkey, bytes32 messageHash, bool messageBound, uint256 threshold, uint256 total, uint64 deadline, bool enforceSharesCheck, address verifierOverride, uint256 state, uint256 commitsCount, uint256 sharesCount, uint256 refusalCount, uint256 purpose, address originContract, uint256 originId, uint16 networkId, bytes32 poolId, uint256 dkgSharesCount)",
            "function advancePhase(uint256 sessionId) external",
            "function finalizeDKG(uint256 sessionId, bytes calldata groupPubkey) external",
            "function MIN_THRESHOLD() external view returns (uint256)",
            "function MAX_PARTICIPANTS() external view returns (uint256)",
            "function SESSION_TIMEOUT() external view returns (uint256)",
            "event SessionCreated(uint256 indexed sessionId, address initiator, uint8 purpose, uint32 threshold, uint256 deadline)",
            "event SessionOpened(uint256 indexed sessionId, address indexed initiator, uint8 indexed purpose)",
            "function submitNonceCommit(uint256 sessionId, bytes32 commitment) external",
            "function submitDKGShare(uint256 sessionId, address recipient, bytes calldata encryptedShare) external",
            "function sessionParticipants(uint256 sessionId, uint256 index) external view returns (address)",
            "function getNonceCommitment(uint256 sessionId, address participant) external view returns (bytes32)",
            "function getDKGShare(uint256 sessionId, address sender, address recipient) external view returns (bytes)"
        ],

        // ИСПРАВЛЕННЫЙ ABI ДЛЯ MINING POOL CORE
        MINING_POOL_CORE: [
            // События
            "event Initialized(uint64 version)",
            "event ParticipantRegistered(address indexed user)",
            "event PayoutScriptSet(bytes script)",
            "event PolicySet(address policy)",
            "event RewardRegistered(bytes32 indexed utxoKey, bytes32 txid, uint32 vout, uint64 amountSat, bytes32 blockHash)",
            "event RoleAdminChanged(bytes32 indexed role, bytes32 indexed previousAdminRole, bytes32 indexed newAdminRole)",
            "event RoleGranted(bytes32 indexed role, address indexed account, address indexed sender)",
            "event RoleRevoked(bytes32 indexed role, address indexed account, address indexed sender)",
            "event SharesUpdated(address indexed miner, uint256 shares)",

            // Константы и роли
            "function ADMIN_ROLE() external view returns (bytes32)",
            "function COINBASE_MATURITY() external view returns (uint64)",
            "function CONFIRMER_ROLE() external view returns (bytes32)",
            "function DEFAULT_ADMIN_ROLE() external view returns (bytes32)",
            "function POOL_MANAGER_ROLE() external view returns (bytes32)",

            // Основные функции пула
            "function availableFor(address beneficiary) external view returns (uint256)",
            "function calculatorRegistry() external view returns (address)",
            "function claimedBalance(address) external view returns (uint256)",
            "function distributeRewardsStrict(uint256 totalAmount, uint256 periodId, bytes blockHeaderRaw, bytes txRaw, uint32 vout, bytes32[] merkleProof, uint8[] directions) external",
            "function extensionsContract() external view returns (address)",
            "function frostCoordinator() external view returns (address)",
            "function getRoleAdmin(bytes32 role) external view returns (bytes32)",
            "function getUTXO(bytes32 utxoKey) external view returns (tuple(bytes32 txid, uint32 vout, uint64 amountSat, bytes32 blockHash, bool spent, bool registered, bool reserved, uint256 reservedBy))",
            "function grantRole(bytes32 role, address account) external",
            "function groupPubkeyX() external view returns (uint256)",
            "function groupPubkeyY() external view returns (uint256)",
            "function hasRole(bytes32 role, address account) external view returns (bool)",
            "function initialize(address spvAddress, address frostAddress, address _calculatorRegistry, address _stratumAggregator, address _stratumValidator, address _oracleRegistry, uint256 pubX, uint256 pubY, string _poolId) external",
            "function membershipSBT() external view returns (address)",
            "function minConfirmations() external view returns (uint64)",
            "function minerShares(address) external view returns (uint256)",
            "function multiPoolDAO() external view returns (address)",
            "function oracleRegistry() external view returns (address)",
            "function participants(address) external view returns (bool)",
            "function payoutScript() external view returns (bytes)",
            "function policy() external view returns (address)",
            "function poolId() external view returns (string)",
            "function poolToken() external view returns (address)",
            "function pruneSpentUTXOs(uint256 max) external",
            "function registerParticipant() external",
            "function registerRewardStrict(bytes blockHeaderRaw, bytes txRaw, uint32 vout, bytes32[] merkleProof, uint8[] directions) external",
            "function renounceRole(bytes32 role, address callerConfirmation) external",
            "function revokeRole(bytes32 role, address account) external",
            "function rewardsContract() external view returns (address)",
            "function selectUTXO(uint64 minAmount) external view returns (bytes32 utxoKey, bool found)",
            "function setCalculator(uint256 calculatorId) external",
            "function setExtensionsContract(address _extensionsContract) external",
            "function setGroupPubkey(uint256 pubX, uint256 pubY) external",
            "function setMembershipContracts(address _membershipSBT, address) external",
            "function setMinConfirmations(uint64 conf) external",
            "function setMultiPoolDAO(address _multiPoolDAO) external",
            "function setNewDependencies(address frostAddress, address _calculatorRegistry, address _stratumAggregator, address _stratumValidator, address _oracleRegistry) external",
            "function setPayoutScript(bytes script) external",
            "function setPolicy(address _policy) external",
            "function setPoolToken(address _poolToken) external",
            "function setRewardsContract(address _rewardsContract) external",
            "function setSPVContract(uint8 networkId, address spvAddress) external",
            "function spv() external view returns (address)",
            "function spvContracts(uint8) external view returns (address)",
            "function stratumDataAggregator() external view returns (address)",
            "function stratumDataValidator() external view returns (address)",
            "function supportsInterface(bytes4 interfaceId) external view returns (bool)",
            "function totalShares() external view returns (uint256)",
            "function totalVerifiedBalance() external view returns (uint256)",
            "function updateBalances(address user, uint256 claimedAmount, uint256 totalBalanceDeduction) external",
            "function updateShares(address miner, uint256 shares) external",
            "function updateUTXOState(bytes32 utxoKey, bool spent, bool reserved, uint256 reservedBy) external",
            "function utxoKeys(uint256) external view returns (bytes32)",
            "function verifiedUTXOs(bytes32) external view returns (bytes32 txid, uint32 vout, uint64 amountSat, bytes32 blockHash, bool spent, bool registered, bool reserved, uint256 reservedBy)"
        ],

        MINING_POOL_REWARDS: [
            'function distributeRewards(uint256 blockHeight, address[] participants, uint256[] amounts) external',
            'function claimRewards() external',
            'function getRewardsBalance(address participant) external view returns (uint256)',
            'function getTotalRewards() external view returns (uint256)',
            'function getRewardsHistory(address participant, uint256 limit) external view returns (tuple(uint256 blockHeight, uint256 amount, uint256 timestamp)[])',
            'event RewardsDistributed(uint256 indexed blockHeight, uint256 totalAmount)',
            'event RewardsClaimed(address indexed participant, uint256 amount)'
        ],

        MEMBERSHIP_SBT: [
            "function mint(address to, bytes32 poolId, bytes32 role, string calldata tokenURI_) external returns (uint256 tokenId)",
            "function burn(uint256 tokenId) external",
            "function membershipOf(uint256 tokenId) external view returns (tuple(bytes32 poolId, bytes32 role, uint64 joinTimestamp, bool active))",
            "function payoutOf(uint256 tokenId) external view returns (tuple(address evmPayout, string btcAddress, bytes32 btcXOnlyPub, bytes20 btcPubKeyHash))",
            "function tokenOf(address owner) external view returns (uint256)",
            "function setPayoutInfoWithAddress(uint256 tokenId, address evmPayout, string calldata btcAddress, bytes32 btcXOnlyPub, bytes20 btcPubKeyHash) external",
            "function ownerOf(uint256 tokenId) external view returns (address)",
            "function balanceOf(address owner) external view returns (uint256)",
            "event MembershipMinted(address indexed to, uint256 indexed tokenId, bytes32 indexed poolId)",
            "event PayoutUpdated(uint256 indexed tokenId, string btcAddress)"
        ],

        ROLE_BADGE_SBT: [
            "function defineTemplate(uint256 templateId, tuple(uint8 mode, uint8 asset, uint32 period, uint64 threshold, uint16 percent, uint16 feeBps, bytes32 assetId) calldata params) external",
            "function assignTemplate(uint256 membershipId, uint256 templateId) external",
            "function clearTemplate(uint256 membershipId) external",
            "function templateOf(uint256 templateId) external view returns (tuple(uint8 mode, uint8 asset, uint32 period, uint64 threshold, uint16 percent, uint16 feeBps, bytes32 assetId))",
            "function activeTemplateOf(uint256 membershipId) external view returns (uint256)",
            "function balanceOf(address account, uint256 id) external view returns (uint256)",
            'function mint(address to, uint256 templateId, uint256 amount) external',
            'function createTemplate(uint8 mode, uint8 asset, uint256 threshold, uint256 timelock) external returns (uint256)',
            'function setActiveTemplate(uint256 membershipId, uint256 templateId) external',
            'event TemplateMinted(address indexed to, uint256 indexed templateId, uint256 amount)',
            'event TemplateCreated(uint256 indexed templateId, uint8 mode, uint8 asset)',
            'event ActiveTemplateSet(uint256 indexed membershipId, uint256 indexed templateId)'
        ],

        CALCULATOR_REGISTRY: [
            "function registerCalculator(uint256 calculatorId, address calculatorAddress, string name, string description) external",
            "function getCalculator(uint256 calculatorId) external view returns (address)",
            "function getCalculatorInfo(uint256 calculatorId) external view returns (tuple(address calculatorAddress, string name, string description, bool active))",
            "function getCalculatorsByType(enum CalculatorRegistry.SchemeType schemeType) external view returns (uint256[])",
            "function getCalculatorCount() external view returns (uint256)",
            "function setCalculatorStatus(uint256 calculatorId, bool active) external",
            "event CalculatorRegistered(uint256 indexed calculatorId, address indexed calculatorAddress, string name)",
            "event CalculatorUpdated(uint256 indexed calculatorId, string parameter, string newValue)"
        ],

        TOKEN_FACTORY: [
            "function createMpToken(string name, string symbol, address poolDAO) external returns (address tokenAddr)",
            "function createMpTokenRestricted(string name, string symbol, address poolDAO) external returns (address tokenAddr)",
            "function createSToken(string name, string symbol, address multiPoolDAO) external returns (address tokenAddr)",
            "function getTokenAddress(string name, string symbol, address creator) external view returns (address)",
            "event MpTokenCreated(address indexed token, string name, string symbol, address indexed poolDAO, address indexed creator)",
            "event STokenCreated(address indexed token, string name, string symbol, address indexed multiPoolDAO, address indexed creator)"
        ],

        ORACLE_REGISTRY: [
            'function registerProvider(address provider, string endpoint, uint256 stake) external',
            'function submitData(bytes32 poolId, uint256 timestamp, bytes data) external',
            'function aggregateData(bytes32 poolId, uint256 timestamp) external view returns (bytes)',
            'function getProviderInfo(address provider) external view returns (tuple(string endpoint, uint256 stake, bool active, uint256 reputation))',
            'event ProviderRegistered(address indexed provider, string endpoint)',
            'event DataSubmitted(address indexed provider, bytes32 indexed poolId, uint256 timestamp)',
            'event DataAggregated(bytes32 indexed poolId, uint256 timestamp, bytes aggregatedData)'
        ]
    },

    // API Endpoints
    API: {
        MINING: 'https://api-mining.unilayer.solutions/api',
        ENDPOINTS: {
            WORKERS: '/workers',
            BLOCKS: '/blocks',
            SHARES: '/shares',
            HEALTH: '/health',
            STATS: '/stats',
            POOLS: '/pools',
            MINING_START: '/mining/start',
            MINING_STOP: '/mining/stop'
        }
    },

    // Reward Calculators
    CALCULATORS: {
        PPLNS: 0,
        PPS: 1,
        FPPS: 2,
        SCORE: 3
    },

    // Bitcoin Network IDs
    NETWORKS_BTC: {
        BTC: 0,
        BCH: 1,
        LTC: 2,
        DOGE: 3
    },

    // FROST Session Purposes
    FROST_PURPOSES: {
        UNKNOWN: 0,
        WITHDRAWAL: 1,
        SLASH: 2,
        REDEMPTION: 3,
        BRIDGE_OUT: 4,
        BRIDGE_IN: 5,
        DKG: 6
    }
};

// Export for use in other modules
window.CONFIG = CONFIG;
