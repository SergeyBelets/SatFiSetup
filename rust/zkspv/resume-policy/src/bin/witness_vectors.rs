#[path = "../../tests/support/mod.rs"] mod support;
use satoshifi_resume_policy::{archive::Archive,Witness,encode_witnesses};
fn hex(bytes:&[u8])->String {format!("0x{}",bytes.iter().map(|b|format!("{b:02x}")).collect::<String>())}
fn main() {
    let mut archive=Archive::new(&support::checkpoint());let anchor=archive.state().view.anchor;
    println!("{{\"schema\":\"satoshifi.resume-index-v2\",\"cases\":[");
    for i in 0..2 {
        if i>0 {support::add(&mut archive,anchor,1,1);println!(",");}
        let state=archive.state();let opening=archive.opening(state.view.head).unwrap();
        let wire=encode_witnesses(&[Witness::Known(opening)]).unwrap();let v=&state.view;
        println!("{{\"wire\":\"{}\",\"anchor\":\"{}\",\"anchor_height\":{},\"known_root\":\"{}\",\"known_log\":\"{}\",\"known_count\":{},\"checkpoint_descriptor\":\"{}\",\"canonical_root\":\"{}\",\"head\":\"{}\",\"height\":{},\"work\":\"{}\",\"timestamp\":{},\"state_commitment\":\"{}\"}}",
            hex(&wire),hex(&v.anchor),v.anchor_height,hex(&state.known_root),hex(&state.known_log),state.known_count,hex(&state.checkpoint_descriptor),
            hex(&v.canonical_root),hex(&v.head),v.height,hex(&v.work),v.timestamp,hex(&v.state_commitment));
    }
    println!("]}}");
}
