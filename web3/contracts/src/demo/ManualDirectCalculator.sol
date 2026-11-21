// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./IDistributionScheme.sol";

/**
 * @title ManualDirectCalculator
 * @notice Калькулятор для ручного распределения наград с ПРЯМЫМИ СУММАМИ
 * @dev Используется для пулов БЕЗ Stratum - менеджер загружает CSV с точными суммами:
 *      CSV формат: bitcoinAddress,ethereumAddress,amountSats
 *      Пример:
 *        bc1q...,0xAlice...,100000000  (1 BTC)
 *        bc1q...,0xBob...,50000000     (0.5 BTC)
 *
 *      Остаток от распределения отправляется на указанный адрес (remainderRecipient)
 *
 * ВАЖНО: Этот калькулятор - ЗАГЛУШКА вместо Stratum.
 *        Он НЕ ломает логику других калькуляторов (FPPS, PPLNS).
 */
contract ManualDirectCalculator is IDistributionScheme {

    // ========================================
    // КОНСТАНТЫ
    // ========================================

    uint256 public constant MAX_RECIPIENTS = 10000;  // Максимум получателей в одной транзакции
    uint256 public constant MIN_AMOUNT = 1;          // Минимум 1 satoshi

    // ========================================
    // СОСТОЯНИЕ
    // ========================================

    /// @notice Адрес для остатка (remainder)
    address public remainderRecipient;

    /// @notice Владелец калькулятора (может менять remainderRecipient)
    address public owner;

    // ========================================
    // СОБЫТИЯ
    // ========================================

    event RemainderRecipientUpdated(address indexed oldRecipient, address indexed newRecipient);
    event ManualDistributionCalculated(uint256 totalAmount, uint256 distributedAmount, uint256 remainder, uint256 recipients);

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
     * @notice Распределение с ПРЯМЫМИ СУММАМИ
     * @dev Использует WorkerData.validShares как прямую сумму в satoshis
     *
     * ВАЖНО: В CSV/фронте указываются ТОЧНЫЕ СУММЫ:
     *   Worker1.validShares = 100000000 (1 BTC)
     *   Worker2.validShares = 50000000  (0.5 BTC)
     *
     * Остаток (если есть) отправляется на remainderRecipient
     *
     * @param totalAmount Общая сумма награды (в satoshis)
     * @param workers Массив получателей с суммами в validShares
     * @param params Параметры (не используются в этом калькуляторе)
     * @return results Результаты распределения
     * @return distributedAmount Распределённая сумма
     * @return remainder Остаток (отправится на remainderRecipient)
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

        // Подсчёт общей запрошенной суммы
        uint256 totalRequested = 0;
        for (uint256 i = 0; i < workers.length; i++) {
            if (workers[i].validShares > 0 && workers[i].payoutAddress != address(0)) {
                totalRequested += workers[i].validShares;
            }
        }

        // Проверка: не превышает ли запрошенная сумма доступную
        require(totalRequested <= totalAmount, "Requested amount exceeds total");

        // Создаём результаты (activeCount + 1 для remainder)
        bool hasRemainder = totalRequested < totalAmount;
        uint256 resultsCount = hasRemainder ? activeCount + 1 : activeCount;
        results = new DistributionResult[](resultsCount);

        // Распределяем точные суммы
        uint256 resultIndex = 0;
        for (uint256 i = 0; i < workers.length; i++) {
            if (workers[i].validShares > 0 && workers[i].payoutAddress != address(0)) {
                uint256 amount = workers[i].validShares;

                results[resultIndex] = DistributionResult({
                    recipient: workers[i].payoutAddress,
                    amount: amount,
                    workerId: workers[i].workerId,
                    percentage: (amount * 10000) / totalAmount  // в базисных пунктах
                });

                distributedAmount += amount;
                resultIndex++;
            }
        }

        // Остаток отправляется на remainderRecipient
        remainder = totalAmount - distributedAmount;
        if (hasRemainder) {
            results[resultIndex] = DistributionResult({
                recipient: remainderRecipient,
                amount: remainder,
                workerId: "REMAINDER",
                percentage: (remainder * 10000) / totalAmount
            });
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

        uint256 totalRequested = 0;
        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress != address(0)) {
                totalRequested += members[i].aggregatedValidShares;
            }
        }

        require(totalRequested <= totalAmount, "Requested amount exceeds total");

        bool hasRemainder = totalRequested < totalAmount;
        uint256 resultsCount = hasRemainder ? activeCount + 1 : activeCount;
        results = new DistributionResult[](resultsCount);

        uint256 resultIndex = 0;
        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress != address(0)) {
                uint256 amount = members[i].aggregatedValidShares;

                results[resultIndex] = DistributionResult({
                    recipient: members[i].payoutAddress,
                    amount: amount,
                    workerId: "",
                    percentage: (amount * 10000) / totalAmount
                });

                distributedAmount += amount;
                resultIndex++;
            }
        }

        remainder = totalAmount - distributedAmount;
        if (hasRemainder) {
            results[resultIndex] = DistributionResult({
                recipient: remainderRecipient,
                amount: remainder,
                workerId: "REMAINDER",
                percentage: (remainder * 10000) / totalAmount
            });
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

        // Проверка общей запрошенной суммы и валидация каждого воркера
        uint256 totalRequested = 0;
        for (uint256 i = 0; i < workers.length; i++) {
            if (workers[i].validShares > 0 && workers[i].payoutAddress == address(0)) {
                return (false, "Invalid payout address");
            }

            // Проверка минимальной суммы
            if (workers[i].validShares > 0 && workers[i].validShares < MIN_AMOUNT) {
                return (false, "Amount below minimum");
            }

            // Подсчёт общей запрошенной суммы
            if (workers[i].validShares > 0) {
                totalRequested += workers[i].validShares;
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

        // Проверка: не превышает ли запрошенная сумма доступную
        if (totalRequested > totalAmount) {
            return (false, "Requested amount exceeds total");
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

        for (uint256 i = 0; i < members.length; i++) {
            if (members[i].aggregatedValidShares > 0 && members[i].payoutAddress == address(0)) {
                return (false, "Invalid payout address");
            }

            if (members[i].aggregatedValidShares > 0 && members[i].aggregatedValidShares < MIN_AMOUNT) {
                return (false, "Amount below minimum");
            }
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
            "ManualDirect",
            "1.0.0",
            "Manual distribution with direct amounts in satoshis. CSV format: btcAddr,ethAddr,amountSats"
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
