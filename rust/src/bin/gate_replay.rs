//! Deterministic engine overhead only. No models or network.
use sg_voice_prototype::core::Gate;
use std::{hint::black_box, time::Instant};
fn main() {
    println!("sample,turns,elapsed_ns,ns_per_turn");
    for sample in 0..31 {
        let now = Instant::now();
        for _ in 0..10000 {
            let mut g = Gate::new(48000);
            g.input(black_box(&[1; 160]));
            let a = g.start_speculation();
            for n in 1..=4 {
                g.push(a, vec![n; 240]).unwrap();
            }
            assert!(g.next_pcm(a).unwrap().is_none());
            g.authorize_final(a).unwrap();
            for n in 1..=4 {
                assert_eq!(black_box(g.next_pcm(a).unwrap().unwrap()), vec![n; 240]);
            }
            g.generation_end(a).unwrap();
            g.playback_complete(a).unwrap();
        }
        let ns = now.elapsed().as_nanos();
        println!("{sample},10000,{ns},{}", ns / 10000);
    }
}
