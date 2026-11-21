#[path = "../../tests/support/mod.rs"] mod support;
fn hex(bytes:&[u8])->String { format!("0x{}",bytes.iter().map(|b|format!("{b:02x}")).collect::<String>()) }
fn main() {
    println!("{{\"schema\":\"satoshifi.transition-journal.v1\",\"cases\":[");
    for (i,(name,result)) in support::scenarios().into_iter().enumerate() {
        if i>0 { println!(","); }
        let j=result.journal;
        println!("{{\"name\":\"{name}\",\"journal\":\"{}\",\"useful\":{},\"halted\":{},\"processed\":{},\"new_headers\":{},\"maximum\":{},\"tip_time\":{},\"halt_position\":{}}}",
            hex(&j.encode_abi()),j.useful(),j.after().halted,j.processed(),j.time_summary().count(),j.time_summary().maximum(),j.after().timestamp,j.halt_position());
    }
    println!("]}}");
}
