const { ethers } = require("hardhat");

async function main() {
    console.log("🔍 Checking Factory V6 Debug Roles and Access...\n");

    const FACTORY_V6_DEBUG_ADDRESS = "0x592153a6550515D4775a4c2866909e8cd1519898";

    const [deployer] = await ethers.getSigners();
    console.log("Checking account:", deployer.address);

    try {
        const factoryArtifact = await ethers.getContractFactory("MiningPoolFactoryV6");
        const factory = factoryArtifact.attach(FACTORY_V6_DEBUG_ADDRESS);

        console.log("📋 Checking roles...");

        // Получаем роли
        const ADMIN_ROLE = await factory.ADMIN_ROLE();
        const DEFAULT_ADMIN_ROLE = await factory.DEFAULT_ADMIN_ROLE();

        console.log(`ADMIN_ROLE: ${ADMIN_ROLE}`);
        console.log(`DEFAULT_ADMIN_ROLE: ${DEFAULT_ADMIN_ROLE}`);

        // Проверяем роли deployer
        const hasDefaultAdmin = await factory.hasRole(DEFAULT_ADMIN_ROLE, deployer.address);
        const hasAdmin = await factory.hasRole(ADMIN_ROLE, deployer.address);

        console.log(`\n👤 Deployer roles:`);
        console.log(`  Has DEFAULT_ADMIN_ROLE: ${hasDefaultAdmin}`);
        console.log(`  Has ADMIN_ROLE: ${hasAdmin}`);

        if (!hasAdmin) {
            console.log("\n❌ ПРОБЛЕМА: Deployer не имеет ADMIN_ROLE!");
            console.log("🔧 Решение: Нужно выдать ADMIN_ROLE deployer'у");

            if (hasDefaultAdmin) {
                console.log("\n🛠️ Выдаем ADMIN_ROLE...");
                const grantTx = await factory.grantRole(ADMIN_ROLE, deployer.address);
                console.log(`Transaction: ${grantTx.hash}`);
                await grantTx.wait();
                console.log("✅ ADMIN_ROLE выдана!");

                // Проверяем еще раз
                const hasAdminNow = await factory.hasRole(ADMIN_ROLE, deployer.address);
                console.log(`✅ Проверка: Has ADMIN_ROLE now: ${hasAdminNow}`);
            } else {
                console.log("❌ Deployer не имеет прав для выдачи ролей!");
            }
        } else {
            console.log("✅ Deployer имеет необходимые роли");
        }

        // Проверяем модификатор nonReentrant
        console.log("\n🔍 Проверяем дополнительную диагностику...");

        // Попробуем статический вызов для диагностики
        try {
            console.log("🧪 Пробуем статический вызов createPool...");

            const poolParams = {
                asset: "BTC",
                poolId: `static-test-${Date.now()}`,
                mpName: "Static Test Pool",
                mpSymbol: "STATIC",
                restrictedMp: false,
                create2Salt: "0x0000000000000000000000000000000000000000000000000000000000000000",
                payoutScript: "0x76a91494a9f9be309c9920578455363f02a93e405caba088ac",
                calculatorId: 0
            };

            // Статический вызов (не выполняет транзакцию, только проверяет)
            const staticResult = await factory.createPool.staticCall(poolParams);
            console.log("✅ Статический вызов успешен:", staticResult);

        } catch (staticError) {
            console.log("❌ Статический вызов не удался:");
            console.log("  Error:", staticError.message);

            if (staticError.reason) {
                console.log("  Reason:", staticError.reason);
            }

            // Попробуем понять точную ошибку
            if (staticError.data) {
                console.log("  Data:", staticError.data);
            }
        }

        // Проверим состояние ReentrancyGuard
        console.log("\n🔒 Дополнительные проверки...");

        // Проверим что все implementation контракты установлены
        const coreImpl = await factory.coreImplementationV2();
        const rewardsImpl = await factory.rewardsImplementationV2();
        const extensionsImpl = await factory.extensionsImplementationV2();
        const redemptionImpl = await factory.redemptionImplementationV2();

        console.log("Implementation контракты:");
        console.log(`  Core: ${coreImpl}`);
        console.log(`  Rewards: ${rewardsImpl}`);
        console.log(`  Extensions: ${extensionsImpl}`);
        console.log(`  Redemption: ${redemptionImpl}`);

        const allImplSet = coreImpl !== ethers.ZeroAddress &&
        rewardsImpl !== ethers.ZeroAddress &&
        extensionsImpl !== ethers.ZeroAddress &&
        redemptionImpl !== ethers.ZeroAddress;

        console.log(`✅ All implementations set: ${allImplSet}`);

    } catch (error) {
        console.log("❌ Error checking roles:", error.message);
        if (error.data) {
            console.log("Error data:", error.data);
        }
    }
}

main()
.then(() => process.exit(0))
.catch((error) => {
    console.error("Script error:", error);
    process.exit(1);
});
