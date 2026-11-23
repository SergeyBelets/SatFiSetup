// Pinned local solc standard-JSON build. No network, downloads or test bypasses.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [modulePath,output,install,baselinePath]=process.argv.slice(2);if(!modulePath||!output||install&&!['--install','--verifiers'].includes(install))throw Error('solc module and output required');
const solc=require(path.resolve(modulePath));if(!solc.version().startsWith('0.8.39+commit.ce47f2c8.'))throw Error('SOLC_PIN');
const root=path.resolve('web3');
function walk(folder){return fs.readdirSync(path.join(root,folder),{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(folder+'/'+e.name):e.name.endsWith('.sol')?[folder+'/'+e.name]:[]);}
const roots=[...walk('contracts/src'),'test/fixtures/FamilyRegistryHarness.sol'];
const sources=Object.fromEntries(roots.map(p=>[p,{content:fs.readFileSync(path.join(root,p),'utf8')}]));
const input={language:'Solidity',sources,settings:{optimizer:{enabled:true,runs:1},viaIR:true,evmVersion:'osaka',
 remappings:['@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/','@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/','forge-std/=lib/openzeppelin-contracts/lib/forge-std/src/'],
 outputSelection:{'*':{'*':['abi','metadata','evm.bytecode.object','evm.bytecode.linkReferences','evm.deployedBytecode.object','evm.deployedBytecode.linkReferences','evm.deployedBytecode.immutableReferences']}}}};
const closure=Object.fromEntries(roots.map(p=>[p,crypto.createHash('sha256').update(sources[p].content).digest('hex')]));
let baseline=null,incremental=null;
if(baselinePath){
 baseline=JSON.parse(fs.readFileSync(baselinePath,'utf8'));if(baseline.compiler!==solc.version())throw Error('BASELINE_COMPILER');
 for(const k of ['optimizer','viaIR','evmVersion','remappings'])if(JSON.stringify(baseline.settings[k])!==JSON.stringify(input.settings[k]))throw Error('BASELINE_SETTINGS');
 const changed=new Set(Object.entries(baseline.source_sha256).filter(([p,h])=>!fs.existsSync(path.join(root,p))||crypto.createHash('sha256').update(fs.readFileSync(path.join(root,p))).digest('hex')!==h).map(([p])=>p));
 const selected=new Set(roots.filter(p=>!baseline.contracts[p]));
 for(const [p,cs] of Object.entries(baseline.contracts))for(const c of Object.values(cs)){
  const meta=JSON.parse(c.metadata||'{}');if(Object.keys(meta.sources||{}).some(s=>changed.has(s)))selected.add(p);
 }
 input.settings.outputSelection=Object.fromEntries([...selected].sort().map(p=>[p,{'*':['abi','metadata','evm.bytecode.object','evm.bytecode.linkReferences','evm.deployedBytecode.object','evm.deployedBytecode.linkReferences','evm.deployedBytecode.immutableReferences']} ]));
 incremental={baseline_sha256:crypto.createHash('sha256').update(fs.readFileSync(baselinePath)).digest('hex'),changed_sources:[...changed].sort(),compiled_sources:[...selected].sort()};
}
const result=JSON.parse(solc.compile(JSON.stringify(input),{import:p=>{
 const target=path.resolve(root,p);if(!target.startsWith(root+path.sep))return{error:'IMPORT_ESCAPE'};
 try{const contents=fs.readFileSync(target,'utf8');closure[p]=crypto.createHash('sha256').update(contents).digest('hex');return{contents}}catch{return{error:'MISSING_IMPORT:'+p}};
}}));
const errors=(result.errors||[]).filter(e=>e.severity==='error');if(errors.length){process.stderr.write(errors.map(e=>e.formattedMessage).join('\n'));process.exit(1);}
if(baseline)result.contracts={...baseline.contracts,...result.contracts};
const sizes={};for(const p of roots)for(const [name,c] of Object.entries(result.contracts[p]||{})){
 const bytes=c.evm.deployedBytecode.object.length/2;if(bytes>24576)throw Error('EIP170:'+name+':'+bytes);
 if(c.evm.bytecode.object.length/2>49152)throw Error('EIP3860:'+name);sizes[name]=bytes;
}
fs.writeFileSync(output,JSON.stringify({compiler:solc.version(),input_sha256:crypto.createHash('sha256').update(JSON.stringify(input)).digest('hex'),source_sha256:closure,settings:input.settings,incremental,sizes,contracts:result.contracts}));
if(install)for(const p of roots)for(const [name,c] of Object.entries(result.contracts[p]||{})){
 if(install==='--verifiers'&&!['FROSTVerifier','BitcoinFeeFundingVerifier','BitcoinFeePlanVerifier'].includes(name))continue;
 const folder=path.join(root,'contracts/out',path.basename(p));fs.mkdirSync(folder,{recursive:true});
 // Foundry presents identical ABI items in a different order from solc-js.
 // Preserve an existing client presentation only after semantic equality.
 const stable=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);
 const client=path.resolve('signer-agent/contracts',name+'.json');let abi=c.abi;
 if(fs.existsSync(client)) {const old=JSON.parse(fs.readFileSync(client,'utf8'));
  if(JSON.stringify(old.map(stable).sort())===JSON.stringify(abi.map(stable).sort()))abi=old;}
 fs.writeFileSync(path.join(folder,name+'.json'),JSON.stringify({abi,metadata:c.metadata,bytecode:c.evm.bytecode,deployedBytecode:c.evm.deployedBytecode}));
}
process.stdout.write(JSON.stringify({compiler:solc.version(),sizes})+'\n');
