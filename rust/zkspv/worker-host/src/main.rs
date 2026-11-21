//! Native witness host. A resident process caches validated public archive state;
//! cold loads replay admitted headers using the unchanged pinned mainnet kernel.
use satoshifi_mainnet_guest_kernel::{execute, verify_mainnet, transition};
use transition::{bounded::{archive::Archive, checkpoint::ValidatedCheckpoint, State}, Deployment, Request};
use serde_json::{json, Value};
use std::{collections::BTreeSet,fs,io::{self,Read,BufRead,Write},path::Path};
use bitcoin::{consensus::deserialize,block::Header,hashes::Hash as _};
fn raw(v:&Value)->Vec<u8>{let s=v.as_str().expect("hex");assert!(s.len()%2==0&&s.len()<=160);(0..s.len()).step_by(2).map(|i|u8::from_str_radix(&s[i..i+2],16).unwrap()).collect()}
fn hex(b:&[u8])->String{b.iter().map(|x|format!("{x:02x}")).collect()}
fn number(v:&Value)->u64{v.as_str().expect("decimal").parse().unwrap()}
fn state(s:&State)->Value{let v=&s.view;json!([hex(&v.anchor),v.anchor_height.to_string(),hex(&v.head),v.height.to_string(),hex(&v.work),v.timestamp.to_string(),hex(&v.canonical_root),hex(&v.state_commitment),v.halted,hex(&s.known_root),hex(&s.known_log),s.known_count.to_string()])}
fn handle(v:Value,out:&Path,cached:&mut Option<Archive>){
    let prior:Vec<_>=v["prior"].as_array().unwrap().iter().map(raw).collect();
    let checkpoint=ValidatedCheckpoint::validate(&raw(&v["checkpoint"]),number(&v["height"]),raw(&v["work"]).try_into().unwrap(),&prior).expect("checkpoint");
    let persistent=v.get("archive_file").is_some();
    let hit=persistent&&cached.as_ref().is_some_and(|a|a.state().checkpoint_descriptor==checkpoint.descriptor_commitment()&&state(a.state())==v["expected_before"]);
    if !hit{
        let mut a=Archive::new(&checkpoint);
        if persistent{
            let count=number(&v["archive_count"]);assert!(count<=2_000_000);
            let file=fs::File::open(v["archive_file"].as_str().unwrap()).unwrap();assert_eq!(file.metadata().unwrap().len(),count*80);
            let mut reader=io::BufReader::new(file);let mut remaining=count;
            while remaining>0{let n=remaining.min(64);let mut batch=vec![];for _ in 0..n{let mut h=vec![0;80];reader.read_exact(&mut h).unwrap();batch.push(h);}a.prepare_batch(&batch,verify_mainnet).expect("archive replay");remaining-=n;}
            assert_eq!(a.state().known_count,count+1);assert_eq!(state(a.state()),v["expected_before"],"archive is not accepted state");
        }else{
            let history:Vec<_>=v["archive"].as_array().unwrap().iter().map(raw).collect();assert!(history.len()<=200_000);
            for chunk in history.chunks(64){a.prepare_batch(chunk,verify_mainnet).expect("archive replay");}
        }
        *cached=Some(a);
    }
    let archive=cached.as_mut().unwrap();let before=archive.state().clone();
    let headers:Vec<_>=v["headers"].as_array().unwrap().iter().map(raw).collect();assert!(headers.len()<=64);
    let mut candidates=vec![];let mut unique=BTreeSet::new();
    for h in &headers{let header:Header=deserialize(h).unwrap();let hash=header.block_hash().to_byte_array();if archive.entry(hash).is_none()&&unique.insert(hash){candidates.push((hash,h.clone()));}}
    fs::create_dir_all(out).unwrap();
    if !headers.is_empty(){
        let witnesses=archive.prepare_batch(&headers,verify_mainnet).expect("candidate headers");
        let mut chain=[0u8;32];chain[24..].copy_from_slice(&number(&v["chain_id"]).to_be_bytes());
        let request=Request{deployment:Deployment{evm_chain_id:chain,state_address:raw(&v["state"]).try_into().unwrap(),checkpoint_descriptor:before.checkpoint_descriptor},expected:before.view.state_commitment,before:before.clone(),headers,witnesses};
        let encoded=request.encode().unwrap();let result=execute(&encoded).expect("static mainnet kernel");assert_eq!(&result.state,archive.state());
        let mut frame=(encoded.len()as u32).to_le_bytes().to_vec();frame.extend(encoded);fs::write(out.join("state.input"),frame).unwrap();fs::write(out.join("state.journal"),result.journal.encode_abi()).unwrap();
    }
    let heights=v["memberships"].as_array().unwrap();assert!(heights.len()<=4096);
    let memberships:Vec<_>=heights.iter().filter_map(|h|archive.membership(archive.state().view.head,number(h))).map(|m|json!({"height":m.height.to_string(),"header":hex(&m.raw_header),"siblings":m.siblings.iter().map(|s|hex(s)).collect::<Vec<_>>()})).collect();
    let mut result=json!({"before":state(&before),"after":state(archive.state()),"memberships":memberships,"resident_cache_hit":hit});
    if persistent{result["delta"]=json!(candidates.into_iter().filter(|(hash,_)|archive.entry(*hash).is_some()).map(|(_,h)|hex(&h)).collect::<Vec<_>>());}
    else{result["archive"]=json!(archive.snapshot_headers().iter().map(|h|hex(h)).collect::<Vec<_>>());}
    fs::write(out.join("native.json"),serde_json::to_vec(&result).unwrap()).unwrap();
}
fn main(){
    let args:Vec<_>=std::env::args().collect();assert_eq!(args.len(),2);
    let mut cache=None;
    if args[1]=="--serve"{
        let mut input=io::stdin().lock();loop{let mut line=String::new();let n=input.by_ref().take(2_000_001).read_line(&mut line).unwrap();if n==0{break;}assert!(n<=2_000_000&&line.ends_with('\n'));
            let v:Value=serde_json::from_str(&line).unwrap();let out=v["output"].as_str().unwrap().to_owned();handle(v,Path::new(&out),&mut cache);println!("{{\"status\":\"PREPARED\"}}");io::stdout().flush().unwrap();}
    }else{let mut bytes=Vec::new();io::stdin().take(40_000_001).read_to_end(&mut bytes).unwrap();assert!(bytes.len()<=40_000_000);handle(serde_json::from_slice(&bytes).unwrap(),Path::new(&args[1]),&mut cache);}
}
