//! Native input construction only. The unchanged mainnet guest authenticates outputs.
use satoshifi_mainnet_guest_kernel::{execute, verify_mainnet, transition};
use transition::{bounded::{archive::Archive, checkpoint::ValidatedCheckpoint}, Deployment, Request};
use std::{fs, path::PathBuf};

const HEADERS: &[u8] = include_bytes!("../fixtures/headers-32246-36288.bin");
const EXPECTED: &[u8] = include_bytes!("../fixtures/expected-32246-36288.bin");
fn header(h:u64)->Vec<u8>{let i=(h-32246) as usize*80;HEADERS[i..i+80].to_vec()}
fn work(h:u64)->[u8;32]{let i=(h-32246) as usize*68+32;EXPECTED[i..i+32].try_into().unwrap()}
fn bits(h:u64)->u32{u32::from_le_bytes(header(h)[72..76].try_into().unwrap())}

#[test]
fn retarget_calculation_and_first_new_epoch_header_are_both_in_guest_input(){
    let cp=ValidatedCheckpoint::validate(&header(32256),32256,work(32256),&(32246..32256).map(header).collect::<Vec<_>>()).unwrap();
    let mut a=Archive::new(&cp);let mut next=32257;
    let directory=PathBuf::from(std::env::var_os("SATOSHIFI_SCENARIO_VECTORS").expect("explicit new output directory"));
    fs::create_dir_all(&directory).unwrap();
    for boundary in [34272,36288] {
        for chunk in (next..boundary-1).map(header).collect::<Vec<_>>().chunks(64){a.prepare_batch(chunk,verify_mainnet).unwrap();}
        let before=a.state().clone();let parent=a.entry(before.view.head).unwrap().context.clone();
        assert_eq!(before.view.height,boundary-2);
        assert_eq!(parent.current_target_bits,bits(boundary-1));
        assert_ne!(bits(boundary-1),bits(boundary));
        let last=verify_mainnet(&parent,&header(boundary-1).try_into().unwrap(),boundary-1).unwrap();
        assert_eq!(last.current_target_bits,bits(boundary));
        let first=verify_mainnet(&last,&header(boundary).try_into().unwrap(),boundary).unwrap();
        assert_eq!(first.epoch_start_time,u32::from_le_bytes(header(boundary)[68..72].try_into().unwrap()));
        let input=vec![header(boundary-1),header(boundary)];let witnesses=a.prepare_batch(&input,verify_mainnet).unwrap();
        let mut chain=[0;32];chain[30..].copy_from_slice(&31337u16.to_be_bytes());
        let r=Request{deployment:Deployment{evm_chain_id:chain,state_address:[0x11;20],checkpoint_descriptor:before.checkpoint_descriptor},expected:before.view.state_commitment,before,headers:input,witnesses};
        let encoded=r.encode().unwrap();let output=execute(&encoded).unwrap();
        assert_eq!(output.state,*a.state());assert_eq!(output.state.view.work,work(boundary));
        assert_eq!(output.journal.processed(),2);assert_eq!(output.journal.time_summary().count(),2);
        let mut framed=(encoded.len() as u32).to_le_bytes().to_vec();framed.extend(encoded);
        fs::write(directory.join(format!("retarget-{boundary}.input")),framed).unwrap();
        fs::write(directory.join(format!("retarget-{boundary}.journal")),output.journal.encode_abi()).unwrap();
        // Actual static mainnet execution must reject an incorrect target at the boundary.
        let mut bad=r.clone();bad.headers[1][72..76].copy_from_slice(&bits(boundary-1).to_le_bytes());
        assert!(execute(&bad.encode().unwrap()).is_err());
        next=boundary+1;
    }
}
