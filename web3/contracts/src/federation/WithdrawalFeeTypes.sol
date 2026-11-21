// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

library WithdrawalFeeTypes {
    uint256 internal constant Q=1e10;
    uint256 internal constant MAX_MONEY=2100000000000000;
    enum Mode { NONE, BTC, MP, S }
    struct Consent {
        bytes32 request; uint256 revision; Mode mode; address pool;
        uint64 cap; uint256 maxTokenDebit; uint64 maxRate; uint64 refundFeeCap;
        uint64 deadline; uint256 cg; uint256 dg; bytes refundScript;
    }
    struct Budget {
        address owner; address token; address pool; Mode mode; uint256 revision;
        uint256 locked; uint64 cap; uint64 maxRate; uint64 refundFeeCap; uint64 deadline;
        bytes32 requestHash; bytes32 root; bytes refundScript; bool closed;
    }
    struct Claim { bytes32 request; bytes32 outpoint; uint64 sats; bytes32 root; }
    struct Control { uint256 sequence; uint256 cg; uint256 dg; uint64 deadline; }
    // Exact per-version consent is the initial allocation profile. No implicit
    // weights, oracle, market conversion or fee assignment by the coordinator.
    struct Charge { bytes32 request; uint256 revision; uint64 sats; }
    struct CreditUse { bytes32 id; bytes32 payer; uint64 fee; uint64 refund; uint32 carryVout; }
    struct Candidate {
        Control control; bytes32 batch; bytes32 previous; bytes32 planHash; bytes raw;
        bytes32[] requests; Charge[] charges; CreditUse[] credits; bytes32 treasury;
    }
    struct Verified {
        bytes32 root; bytes32 txid; bytes32 inputHash; uint256 fee; uint256 tokenBurn;
        uint256 treasuryFee; uint256 inputFree; uint256 dg; bytes32[] freeKeys;
    }
}

interface IWithdrawalFeePayments {
    function credits() external view returns(address);
    function escrow() external view returns(address);
    function hasPlan(bytes32) external view returns(bool);
    function onReserve(bytes32,bytes32,bytes32[] calldata) external;
    function checkSigning(bytes32) external view;
    function feeBurn(bytes32) external view returns(uint256);
    function settle(bytes32,bytes calldata,uint256,bytes32) external;
    function rootForRequest(bytes32) external view returns(bytes32);
}

interface IWithdrawalFeeCredits {
    function payments() external view returns(address);
    function escrow() external view returns(address);
    function addresses() external view returns(address);
    function funding() external view returns(address);
    function scope() external view returns(bytes32);
    function getBudget(bytes32) external view returns(WithdrawalFeeTypes.Budget memory);
    function getClaim(bytes32) external view returns(WithdrawalFeeTypes.Claim memory);
    function claimsAt(bytes32) external view returns(bytes32[] memory);
    function protectedSats(bytes32) external view returns(uint256);
    function refundRequest(bytes32) external view returns(bool);
    function refundOrigin(bytes32) external view returns(bytes32);
    function admitted(bytes32,bytes32) external view returns(bytes32);
    function bindBudget(bytes32,bytes32,uint256) external;
    function lockClaims(bytes32,WithdrawalFeeTypes.CreditUse[] calldata) external;
    function settleBudget(bytes32,uint256) external;
    function settleClaims(bytes32,bytes32,WithdrawalFeeTypes.CreditUse[] calldata) external;
}

interface IWithdrawalFeePool {
    function consentFee(WithdrawalFeeTypes.Consent calldata) external;
    function feeLock(bytes32) external view returns(address,address,uint256);
    function settleFeeBudget(bytes32,address,address,uint256,uint256) external;
}
interface IWithdrawalFeeConsent {
    function consentS(address owner,WithdrawalFeeTypes.Consent calldata consent) external;
}

interface IUserFeeReserve {
    function protocolFeeCap() external view returns(uint64);
    function maxVbytes() external view returns(uint32);
    function creditAmount(bytes32) external view returns(uint256);
    function lockUserFamily(bytes32,bytes32,bytes32[] calldata,uint256,uint256) external;
    function settleUserFamily(bytes32,bytes32,bytes calldata,uint256,bytes32) external;
}
