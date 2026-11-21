const fs = require("node:fs");
const path = require("node:path");
const solc = require("solc");
if (!solc.version().startsWith("0.8.30+")) throw new Error("solc 0.8.30 required");
const file = "CoordinatorFixture.sol";
const source = fs.readFileSync(path.join(__dirname, file), "utf8");
const compiled = JSON.parse(solc.compile(JSON.stringify({
    language: "Solidity",
    sources: { [file]: { content: source } },
    settings: { viaIR: true, optimizer: { enabled: true, runs: 1 }, evmVersion: "prague",
                outputSelection: { "*": { "*": ["abi", "evm.bytecode.object"] } } }
})));
const errors = (compiled.errors || []).filter(error => error.severity === "error");
if (errors.length) throw new Error(errors.map(error => error.formattedMessage).join("\n"));
process.stdout.write(JSON.stringify(compiled.contracts[file].CoordinatorFixture));
