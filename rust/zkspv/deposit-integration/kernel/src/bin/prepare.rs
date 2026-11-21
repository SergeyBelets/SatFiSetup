//! Native execution of the same code as the guest; no host-supplied journal.
use satoshifi_regtest_deposit_kernel::{read_request,execute};
fn main(){
    let a:Vec<_>=std::env::args_os().collect();assert_eq!(a.len(),3,"prepare INPUT OUTPUT_DIR");
    let input=read_request(std::fs::File::open(&a[1]).unwrap()).unwrap();let (journal,paths)=execute(&input).unwrap();
    let out=std::path::PathBuf::from(&a[2]);std::fs::create_dir_all(&out).unwrap();
    std::fs::write(out.join("state.journal"),journal).unwrap();
    for p in paths {let bytes:Vec<_>=p.siblings.into_iter().flatten().collect();std::fs::write(out.join(format!("history-{}.bin",p.height)),bytes).unwrap();}
}
