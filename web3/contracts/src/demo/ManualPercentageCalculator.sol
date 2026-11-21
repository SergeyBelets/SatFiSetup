// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./IDistributionScheme.sol";

/**
 * @title ManualPercentageCalculator
 * @notice Калькулятор для ручного распределения наград с ПРОЦЕНТАМИ
 * @dev Используется для пулов БЕЗ Stratum - менеджер загружает CSV с процентами:
 *      CSV формат: bitcoinAddress,ethereumAddress,percentageBps
 *      Пример:
 *        bc1q...,0xAlice...,4000  (40.00%)
 *        bc1q...,0xBob...,3500    (35.00%)
 *        bc1q...,0xCarol...,2500  (25.00%)
 *
 *      Проценты в базисных пунктах (10000 = 100%)
 *      Остаток от округлений отправляется на указанный адрес (remainderRecipient)
 *
 * ВАЖНО: Этот калькулятор - ЗАГЛУШКА вместо Stratum.
 *        Он НЕ ломает логику других калькуляторов (FPPS, PPLNS).
 */
contract ManualPercentageCalculator is IDistributionScheme {

    // ========================================
    // КОНСТАНТЫ
    // ========================================

    uint256 public constant MAX_RECIPIENTS = 10000;      // Максимум получателей
    uint256 public constant BASIS_POINTS = 10000;        // 100% = 10000 базисных пунктов
    uint256 public constant MIN_PERCENTAGE = 1;          // Минимум 0.01%
    uint256 public constant MAX_TOTAL_PERCENTAGE = 10000; // Максимум 100%

    // ========================================
    // СОСТОЯНИЕ
    // ========================================

    /// @notice Адрес для остатка (remainder от округлений)
    address public remainderRecipient;

    /// @notice Владелец калькулятора
    address public owner;

    // ========================================
    // СОБЫТИЯ
    // ========================================

    event RemainderRecipientUpdated(address indexed oldRecipient, address indexed newRecipient);
    event ManualDistributionCalculated(
        uint256 totalAmount,
        uint256 distributedAmount,
        uint256 remainder,
        uint256 recipients,
        uint256 totalPercentage
    );

    // ========================================
    // МОДИФИКАТОРЫ
    // ========================================

    modifier onlyOwner() {
        require(msg.sender == owner, "Only owner");
        _;
    }

    // ========================================
    // КОНСТРУКТОР
    // ========================================

    constructor(address _remainderRecipient) {
        require(_remainderRecipient != address(0), "Invalid remainder recipient");
        owner = msg.sender;
        remainderRecipient = _remainderRecipient;
        emit RemainderRecipientUpdated(address(0), _remainderRecipient);
    }

    // ========================================
    // ОСНОВНАЯ ЛОГИКА
    // ========================================

    /**
     * @notice Распределение с ПРОЦЕНТАМИ
     * @dev Использует WorkerData.validShares как процент в базисных пунктах (bps)
     *
     * ВАЖНО: В CSV/фронте указываются ПРОЦЕНТЫ в базисных пунктах:
     *   Worker1.validShares = 4000  (40.00%)
     *   Worker2.validShares = 3500  (35.00%)
     *   Worker3.validShares = 2500  (25.00%)
     *   Total: 10000 (100%)
     *
     * Остаток от округлений отправляется на remainderRecipient
     *
     * @param totalAmount Общая сумма награды (в satoshis)
     * @param workers Массив получателей с процентами в validShares
     * @param params Параметры (не используются в этом калькуляторе)
     * @return results Результаты распределения
     * @return distributedAmount Распределённая сумма
     * @return remainder Остаток от округлений
     */
    function calculate(
        uint256 totalAmount,
        WorkerData[] memory workers,
        SchemeParams memory params
    ) external view override returns (
        DistributionResult[] memory results,
        uint256 distributedAmount,
        uint256 remainder
    ) {
        // Валидация
        (bool isValid, string memory error) = validateInput(totalAmount, workers, params);
        require(isValid, error);

        // Подсчёт активных получателей
        uint256 activeCount = 0;
        for (uint256 i = 0; i < workers.length; i++) {
            if (workers[i].validShares > 0 && workers[i].payoutAddress != address(0)) {
                activeCount++;
            }
        }

        require(activeCount > 0, "No recipients");

        // Подсчёт общего процента
        uint256 totalPercentage = 0;
        for (uint256 i = 0; i < workers.length; i++) {
            if (workers[i].validShares > 0 && workers[i].payoutAddress != address(0)) {
                totalPercentage += workers[i].validShares;
            }
        }

        // Проверка: не превышает ли общий процент 100%
        require(totalPercentage <= BASIS_POINTS, "Total percentage exceeds 100%");

        // Создаём результаты (activeCount + 1 для remainder, если есть)
        bool hasRemainder = totalPercentage < BASIS_POINTS;
        uint256 resultsCount = hasRemainder ? activeCount + 1 : activeCount;
        results = new DistributionResult[](resultsCount);

        // Распределяем по процентам
        uint256 resultIndex = 0;
        for (uint256 i = 0; i < workers.length; i++) {
            if (workers[i].validShares > 0 && workers[i].payoutAddress != address(0)) {
                // Расчёт суммы по проценту
                uint256 amount = (totalAmount * workers[i].validShares) / BASIS_POINTS;

                results[resultIndex] = DistributionResult({
                    recipient: workers[i].payoutAddress,
                    amount: amount,
                    workerId: workers[i].workerId,
                    percentage: workers[i].validShares  // уже в базисных пунктах
                });

                distributedAmount += amount;
                resultIndex++;
            }
        }

        // Остаток (от округлений + неиспользованный процент) → remainderRecipient
        remainder = totalAmount - distributedAmount;
        if (remainder > 0) {
            // Если есть остаток, добавляем запись для remainderRecipient
            if (hasRemainder) {
                results[resultIndex] = DistributionResult({
                    recipient: remainderRecipient,
                    amount: remainder,
                    workerId: "REMAINDER",
                    percentage: BASIS_POINTS - totalPercentage  // оставшийся процент
                });
            } else {
                // Если общий процент = 100%, но есть остаток от округлений
                // Создаём новый массив с +1 элементом
                DistributionResult[] memory newResults = new DistributionResult[](activeCount + 1);
                for (uint256 i = 0; i < activeCount; i++) {
                    newResults[i] = results[i];
                }
                newResults[activeCount] = DistributionResult({
                    recipient: remainderRecipient,
                    amount: remainder,
                    workerId: "REMAINDER_ROUNDING",
                    percentage: 0  // остаток от округлений, не процент
                });
                results = newResults;
            }
        }
    }

    // ========================================
    // ПОДДЕРЖКА MEMBERS (DAO)
    // ========================================

    /**
     * @notice Распределение для членов DAO (аналогично workers)
     */
    function calculateForMembers(
        uint256 totalAmount,
        MemberData[] memory members,
        SchemeParams memory params
    ) external view override returns (
        DistributionResult[] memory results,
        uint256 distributedAmount,
        uint256 remainder
    ) {
        (bool isValid, string memory error) = validateMemberInput(totalAmount, members, params);
        require(isValid, error);

        uint256 activeCount = 0;
        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress != address(0)) {
                activeCount++;
            }
        }

        require(activeCount > 0, "No recipients");

        uint256 totalPercentage = 0;
        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress != address(0)) {
                totalPercentage += members[i].aggregatedValidShares;
            }
        }

        require(totalPercentage <= BASIS_POINTS, "Total percentage exceeds 100%");

        bool hasRemainder = totalPercentage < BASIS_POINTS;
        uint256 resultsCount = hasRemainder ? activeCount + 1 : activeCount;
        results = new DistributionResult[](resultsCount);

        uint256 resultIndex = 0;
        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress != address(0)) {
                uint256 amount = (totalAmount * members[i].aggregatedValidShares) / BASIS_POINTS;

                results[resultIndex] = DistributionResult({
                    recipient: members[i].payoutAddress,
                    amount: amount,
                    workerId: "",
                    percentage: members[i].aggregatedValidShares
                });

                distributedAmount += amount;
                resultIndex++;
            }
        }

        remainder = totalAmount - distributedAmount;
        if (remainder > 0) {
            if (hasRemainder) {
                results[resultIndex] = DistributionResult({
                    recipient: remainderRecipient,
                    amount: remainder,
                    workerId: "REMAINDER",
                    percentage: BASIS_POINTS - totalPercentage
                });
            } else {
                DistributionResult[] memory newResults = new DistributionResult[](activeCount + 1);
                for (uint256 i = 0; i < activeCount; i++) {
                    newResults[i] = results[i];
                }
                newResults[activeCount] = DistributionResult({
                    recipient: remainderRecipient,
                    amount: remainder,
                    workerId: "REMAINDER_ROUNDING",
                    percentage: 0
                });
                results = newResults;
            }
        }
    }

    // ========================================
    // ВАЛИДАЦИЯ
    // ========================================

    function validateInput(
        uint256 totalAmount,
        WorkerData[] memory workers,
        SchemeParams memory /* params */
    ) public pure override returns (bool isValid, string memory errorMessage) {
        if (totalAmount == 0) {
            return (false, "Total amount is zero");
        }

        if (workers.length == 0) {
            return (false, "No workers provided");
        }

        if (workers.length > MAX_RECIPIENTS) {
            return (false, "Too many recipients");
        }

        uint256 totalPercentage = 0;

        for (uint256 i = 0; i < workers.length; i++) {
            // Проверка адреса
            if (workers[i].validShares > 0 && workers[i].payoutAddress == address(0)) {
                return (false, "Invalid payout address");
            }

            // Проверка минимального процента
            if (workers[i].validShares > 0 && workers[i].validShares < MIN_PERCENTAGE) {
                return (false, "Percentage below minimum");
            }

            // Подсчёт общего процента
            if (workers[i].validShares > 0) {
                totalPercentage += workers[i].validShares;
            }

            // Проверка на дубликаты
            for (uint256 j = i + 1; j < workers.length; j++) {
                if (workers[i].payoutAddress == workers[j].payoutAddress &&
                    workers[i].validShares > 0 &&
                    workers[j].validShares > 0) {
                    return (false, "Duplicate recipient addresses");
                }
            }
        }

        // Проверка общего процента
        if (totalPercentage > MAX_TOTAL_PERCENTAGE) {
            return (false, "Total percentage exceeds 100%");
        }

        return (true, "");
    }

    function validateMemberInput(
        uint256 totalAmount,
        MemberData[] memory members,
        SchemeParams memory /* params */
    ) public pure override returns (bool isValid, string memory errorMessage) {
        if (totalAmount == 0) {
            return (false, "Total amount is zero");
        }

        if (members.length == 0) {
            return (false, "No members provided");
        }

        if (members.length > MAX_RECIPIENTS) {
            return (false, "Too many recipients");
        }

        uint256 totalPercentage = 0;

        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress == address(0)) {
                return (false, "Invalid payout address");
            }

            if (members[i].aggregatedValidShares > 0 && members[i].aggregatedValidShares < MIN_PERCENTAGE) {
                return (false, "Percentage below minimum");
            }

            if (members[i].aggregatedValidShares > 0) {
                totalPercentage += members[i].aggregatedValidShares;
            }
        }

        if (totalPercentage > MAX_TOTAL_PERCENTAGE) {
            return (false, "Total percentage exceeds 100%");
        }

        return (true, "");
    }

    // ========================================
    // ИНФОРМАЦИЯ О СХЕМЕ
    // ========================================

    function getSchemeInfo() external pure override returns (
        string memory name,
        string memory version,
        string memory description
    ) {
        return (
            "ManualPercentage",
            "1.0.0",
            "Manual distribution with percentage shares (basis points). CSV format: btcAddr,ethAddr,percentageBps"
        );
    }

    function estimateGas(
        uint256 workerCount,
        SchemeParams memory /* params */
    ) external pure override returns (uint256 estimatedGas) {
        // Базовый газ + ~5000 на каждого получателя
        return 50000 + (workerCount * 5000);
    }

    // ========================================
    // УПРАВЛЕНИЕ
    // ========================================

    /**
     * @notice Обновить адрес получателя остатка
     * @param newRecipient Новый адрес для остатка
     */
    function setRemainderRecipient(address newRecipient) external onlyOwner {
        require(newRecipient != address(0), "Invalid address");
        address oldRecipient = remainderRecipient;
        remainderRecipient = newRecipient;
        emit RemainderRecipientUpdated(oldRecipient, newRecipient);
    }

    /**
     * @notice Передать владение калькулятором
     * @param newOwner Новый владелец
     */
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Invalid address");
        owner = newOwner;
    }
}
