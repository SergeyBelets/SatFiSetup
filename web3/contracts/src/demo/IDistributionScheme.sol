// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title IDistributionScheme
 * @notice Интерфейс для схем распределения наград
 */
interface IDistributionScheme {

    struct WorkerData {
        address payoutAddress;
        uint256 validShares;
        string workerId;
    }

    struct MemberData {
        address payoutAddress;
        uint256 aggregatedValidShares;
    }

    struct SchemeParams {
        uint256 param1;
        uint256 param2;
    }

    struct DistributionResult {
        address recipient;
        uint256 amount;
        string workerId;
        uint256 percentage;
    }

    function calculate(
        uint256 totalAmount,
        WorkerData[] memory workers,
        SchemeParams memory params
    ) external view returns (
        DistributionResult[] memory results,
        uint256 distributedAmount,
        uint256 remainder
    );

    function calculateForMembers(
        uint256 totalAmount,
        MemberData[] memory members,
        SchemeParams memory params
    ) external view returns (
        DistributionResult[] memory results,
        uint256 distributedAmount,
        uint256 remainder
    );

    function validateInput(
        uint256 totalAmount,
        WorkerData[] memory workers,
        SchemeParams memory params
    ) external view returns (bool isValid, string memory errorMessage);

    function validateMemberInput(
        uint256 totalAmount,
        MemberData[] memory members,
        SchemeParams memory params
    ) external view returns (bool isValid, string memory errorMessage);

    function getSchemeInfo() external pure returns (
        string memory name,
        string memory version,
        string memory description
    );

    function estimateGas(
        uint256 workerCount,
        SchemeParams memory params
    ) external pure returns (uint256 estimatedGas);
}
