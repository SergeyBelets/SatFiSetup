#[path="../../tests/support.rs"] mod support;
fn hex(b:&[u8])->String {format!("0x{}",b.iter().map(|b|format!("{b:02x}")).collect::<String>())}
fn main() {
    println!("{{\"schema\":\"satoshifi.transition-journal.v2\",\"cases\":[");
    for (i,(name,r)) in support::scenarios().into_iter().enumerate() {
        if i>0 {println!(",");}let j=support::execute(&r).journal;
        println!("{{\"name\":\"{name}\",\"journal\":\"{}\",\"useful\":{},\"halted\":{},\"processed\":{},\"new_headers\":{},\"maximum\":{},\"tip_time\":{},\"halt_position\":{}}}",
            hex(&j.encode_abi()),j.useful(),j.after().view.halted,j.processed(),j.time_summary().count(),j.time_summary().maximum(),j.after().view.timestamp,j.halt_position());
    }
    println!("]}}");
}
