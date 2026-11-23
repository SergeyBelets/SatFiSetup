// Type/ABI compilation of all ordinary Solidity tests; no test execution claim.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [modulePath,output]=process.argv.slice(2);const solc=require(path.resolve(modulePath));
if(solc.version()!=='0.8.39+commit.ce47f2c8.Emscripten.clang')throw Error('SOLC_PIN');
const root=path.resolve('web3');
const walk=p=>fs.readdirSync(path.join(root,p),{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(p+'/'+e.name):e.name.endsWith('.sol')?[p+'/'+e.name]:[]);
const sources={},hashes={};for(const p of [...walk('contracts/src'),...walk('test')]){
 const content=fs.readFileSync(path.join(root,p),'utf8');sources[p]={content};hashes[p]=crypto.createHash('sha256').update(content).digest('hex');
}
const settings={optimizer:{enabled:true,runs:1},viaIR:true,evmVersion:'osaka',remappings:[
 '@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/','@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/','forge-std/=lib/openzeppelin-contracts/lib/forge-std/src/'],outputSelection:{'*':{'*':['abi']}}};
const r=JSON.parse(solc.compile(JSON.stringify({language:'Solidity',sources,settings}),{import:p=>{
 const target=path.resolve(root,p);if(!target.startsWith(root+path.sep))return{error:'IMPORT_ESCAPE'};
 try{const contents=fs.readFileSync(target,'utf8');hashes[p]=crypto.createHash('sha256').update(contents).digest('hex');return{contents}}catch{return{error:'MISSING_IMPORT:'+p}};
}}));
const errors=(r.errors||[]).filter(e=>e.severity==='error');if(errors.length){process.stderr.write(errors.map(e=>e.formattedMessage).join('\n'));process.exit(1);}
const tests=Object.keys(r.contracts).filter(p=>p.startsWith('test/'));const report={status:'TYPE_AND_ABI_COMPILE_PASS',compiler:solc.version(),test_source_count:tests.length,
 contract_count:Object.values(r.contracts).reduce((n,c)=>n+Object.keys(c).length,0),source_sha256:hashes,solidity_tests_executed:false,settings};
fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');process.stdout.write(JSON.stringify({status:report.status,test_source_count:tests.length,contract_count:report.contract_count})+'\n');
