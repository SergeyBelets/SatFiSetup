#[path = "../../tests/support/mod.rs"] mod support;
use satoshifi_checkpoint_policy::fork::Context;
fn hex(bytes: &[u8]) -> String { format!("0x{}",bytes.iter().map(|b|format!("{b:02x}")).collect::<String>()) }
fn main() {
    println!("{{\"schema\":\"satoshifi.mainnet-checkpoint.v1\",\"checkpoints\":[");
    for (i,height) in [32256,34272,36288].into_iter().enumerate() {
        if i>0 { println!(","); }
        let cp=support::checkpoint(height); let c=cp.context();
        let prior=support::prior(height).iter().map(|p|format!("\"{}\"",hex(p))).collect::<Vec<_>>().join(",");
        println!("{{\"height\":{height},\"raw\":\"{}\",\"prior\":[{prior}],\"work\":\"{}\",\"hash\":\"{}\",\"bits\":{},\"epoch_time\":{},\"timestamps\":{:?},\"context_commitment\":\"{}\",\"descriptor_commitment\":\"{}\"}}",
            hex(cp.raw_header()),hex(&c.total_work),hex(&c.block_hash),c.current_target_bits,c.epoch_start_time,c.prev_11_timestamps,
            hex(&c.commitment()),hex(&cp.descriptor_commitment()));
    }
    println!("]}}");
}
