//! Attempt-scoped local audio-source queue completion, never remote audibility.
use std::{future::Future, pin::Pin, sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, oneshot},
    time::{sleep_until, Instant},
};

pub type Capture<'a> = Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>>;
pub trait AudioSink: Send + Sync + 'static {
    fn capture(&self, pcm: Vec<i16>) -> Capture<'_>;
    fn clear(&self);
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Stale,
    Unauthorized,
    Closed,
    Finished,
    InvalidPcm,
    Sink,
    Cancelled,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Completion {
    LocalQueueDrained,
}
type Reply<T> = oneshot::Sender<Result<T, Error>>;
enum Control {
    Begin(u64, Reply<()>),
    Authorize(u64, Reply<()>),
    Cancel(u64, Reply<()>),
    Speech(u64, bool, Reply<()>),
    Shutdown(Reply<()>),
}
enum Audio {
    Pcm(u64, Vec<i16>, Reply<()>),
    End(u64, Reply<Completion>),
}
#[derive(Clone)]
pub struct Playout {
    control: mpsc::Sender<Control>,
    audio: mpsc::Sender<Audio>,
    max_chunk_samples: usize,
    channels: usize,
}
struct State {
    id: Option<u64>,
    highest: u64,
    authorized: bool,
    enabled: bool,
    deadline: Instant,
    ended: bool,
    remainder: usize,
    waiter: Option<Reply<Completion>>,
}
impl State {
    fn valid(&self, id: u64) -> Result<(), Error> {
        if self.id == Some(id) {
            Ok(())
        } else {
            Err(Error::Stale)
        }
    }
    fn clear(&mut self, sink: &dyn AudioSink, reason: Error) {
        sink.clear();
        if let Some(w) = self.waiter.take() {
            let _ = w.send(Err(reason));
        }
        self.id = None;
        self.deadline = Instant::now();
        self.ended = false;
        self.remainder = 0;
        self.authorized = false;
    }
    /// Returns (cancel current capture, shut down).
    fn control(&mut self, c: Control, sink: &dyn AudioSink) -> (bool, bool) {
        match c {
            Control::Begin(id, r) => {
                if id <= self.highest {
                    let _ = r.send(Err(Error::Stale));
                    return (false, false);
                }
                self.clear(sink, Error::Cancelled);
                self.highest = id;
                self.id = Some(id);
                let _ = r.send(Ok(()));
                (true, false)
            }
            Control::Authorize(id, r) => {
                let result = self.valid(id);
                if result.is_ok() {
                    self.authorized = true;
                }
                let _ = r.send(result);
                (false, false)
            }
            Control::Cancel(id, r) => {
                let result = self.valid(id);
                let valid = result.is_ok();
                if valid {
                    self.clear(sink, Error::Cancelled);
                }
                let _ = r.send(result);
                (valid, false)
            }
            Control::Speech(id, enabled, r) => {
                let result = self.valid(id).and({
                    if self.authorized {
                        Ok(())
                    } else {
                        Err(Error::Unauthorized)
                    }
                });
                let abort = result.is_ok() && !enabled;
                if result.is_ok() {
                    self.enabled = enabled;
                    if !enabled {
                        sink.clear();
                        self.deadline = Instant::now();
                        self.remainder = 0;
                        if let Some(w) = self.waiter.take() {
                            let _ = w.send(Err(Error::Cancelled));
                        }
                    }
                }
                let _ = r.send(result);
                (abort, false)
            }
            Control::Shutdown(r) => {
                self.clear(sink, Error::Closed);
                let _ = r.send(Ok(()));
                (true, true)
            }
        }
    }
}
async fn capture_piece(
    s: &mut State,
    sink: &Arc<dyn AudioSink>,
    controls: &mut mpsc::Receiver<Control>,
    pcm: Vec<i16>,
    rate: u32,
    channels: u32,
) -> Result<(), (Error, bool)> {
    let quantum = (rate as usize / 100).max(1) * channels as usize;
    s.remainder = (s.remainder + pcm.len()) % quantum;
    s.deadline = s.deadline.max(Instant::now())
        + Duration::from_secs_f64(pcm.len() as f64 / (rate as f64 * channels as f64));
    // Never await a multi-chunk SDK capture after cancellation: it could submit
    // later stale chunks. Each future covers at most one 10 ms piece.
    let capture = sink.capture(pcm);
    tokio::pin!(capture);
    loop {
        tokio::select! {biased;
            command=controls.recv()=>{
                let Some(c)=command else{return Err((Error::Closed,true));};
                let (abort,stop)=s.control(c,sink.as_ref());
                if abort {
                    // SDK clear_buffer releases an outstanding callback on its
                    // next audio tick. Drain only this one piece before reuse.
                    let cleaned=tokio::time::timeout(Duration::from_millis(100),&mut capture).await.is_ok();
                    return Err((Error::Cancelled,stop || !cleaned));
                }
            },
            result=&mut capture=>{
                if result.is_err(){s.clear(sink.as_ref(),Error::Sink);return Err((Error::Sink,true));}
                return Ok(());
            }
        }
    }
}
impl Playout {
    pub fn spawn(
        sink: Arc<dyn AudioSink>,
        sample_rate: u32,
        channels: u32,
        max_chunk_samples: usize,
    ) -> Self {
        assert!(sample_rate > 0 && channels > 0 && max_chunk_samples > 0);
        let (control, mut controls) = mpsc::channel(8);
        let (audio, mut frames) = mpsc::channel(8);
        tokio::spawn(async move {
            let mut s = State {
                id: None,
                highest: 0,
                authorized: false,
                enabled: true,
                deadline: Instant::now(),
                ended: false,
                remainder: 0,
                waiter: None,
            };
            'actor: loop {
                tokio::select! { biased;
                    c=controls.recv()=>match c {Some(c)=>{if s.control(c,sink.as_ref()).1{break;}},None=>break},
                    _=sleep_until(s.deadline),if s.waiter.is_some()=>{
                        if let Some(w)=s.waiter.take(){let _=w.send(Ok(Completion::LocalQueueDrained));}
                    },
                    frame=frames.recv()=>match frame {
                        Some(Audio::End(id,r))=>{
                            let result=s.valid(id).and(if s.authorized && !s.ended{Ok(())}else{Err(Error::Unauthorized)});
                            if let Err(e)=result{let _=r.send(Err(e));}else{
                                // libwebrtc drains in 10 ms frames. Preserve a final
                                // partial frame by adding only trailing zero samples.
                                let quantum=(sample_rate as usize/100).max(1)*channels as usize;
                                if s.remainder>0 {
                                    let pad=vec![0;quantum-s.remainder];
                                    if let Err((error,closed))=capture_piece(&mut s,&sink,&mut controls,pad,sample_rate,channels).await{let _=r.send(Err(error));if closed{break 'actor;}continue;}
                                }
                                s.ended=true;s.waiter=Some(r);
                            }
                        },
                        Some(Audio::Pcm(id,pcm,r))=>{
                            let valid=s.valid(id).and(if !s.authorized || !s.enabled{Err(Error::Unauthorized)}else if s.ended{Err(Error::Finished)}else{Ok(())});
                            if let Err(e)=valid{let _=r.send(Err(e));continue;}
                            if pcm.is_empty() || pcm.len()>max_chunk_samples || !pcm.len().is_multiple_of(channels as usize){let _=r.send(Err(Error::InvalidPcm));continue;}
                            let mut outcome=Ok(());let mut stop=false;
                            let quantum=(sample_rate as usize/100).max(1)*channels as usize;
                            for chunk in pcm.chunks(quantum) {
                                if let Err((error,closed))=capture_piece(&mut s,&sink,&mut controls,chunk.to_vec(),sample_rate,channels).await {outcome=Err(error);stop=closed;break;}
                            }
                            let _=r.send(outcome);if stop{break 'actor;}
                        },None=>break,
                    }
                }
            }
            s.clear(sink.as_ref(), Error::Closed);
        });
        Self {
            control,
            audio,
            max_chunk_samples,
            channels: channels as usize,
        }
    }
    async fn ctrl(&self, make: impl FnOnce(Reply<()>) -> Control) -> Result<(), Error> {
        let (tx, rx) = oneshot::channel();
        self.control
            .send(make(tx))
            .await
            .map_err(|_| Error::Closed)?;
        rx.await.map_err(|_| Error::Closed)?
    }
    pub async fn begin(&self, id: u64) -> Result<(), Error> {
        self.ctrl(|r| Control::Begin(id, r)).await
    }
    pub async fn authorize(&self, id: u64) -> Result<(), Error> {
        self.ctrl(|r| Control::Authorize(id, r)).await
    }
    pub async fn cancel(&self, id: u64) -> Result<(), Error> {
        self.ctrl(|r| Control::Cancel(id, r)).await
    }
    pub async fn speech_decision(&self, id: u64, enabled: bool) -> Result<(), Error> {
        self.ctrl(|r| Control::Speech(id, enabled, r)).await
    }
    pub async fn push(&self, id: u64, pcm: Vec<i16>) -> Result<(), Error> {
        if pcm.is_empty()
            || pcm.len() > self.max_chunk_samples
            || !pcm.len().is_multiple_of(self.channels)
        {
            return Err(Error::InvalidPcm);
        }
        let (tx, rx) = oneshot::channel();
        self.audio
            .send(Audio::Pcm(id, pcm, tx))
            .await
            .map_err(|_| Error::Closed)?;
        rx.await.map_err(|_| Error::Closed)?
    }
    /// Call after all push acknowledgements. This waits for the local source's
    /// estimated queue drain, not delivery through a remote jitter buffer.
    pub async fn finish(&self, id: u64) -> Result<Completion, Error> {
        let (tx, rx) = oneshot::channel();
        self.audio
            .send(Audio::End(id, tx))
            .await
            .map_err(|_| Error::Closed)?;
        rx.await.map_err(|_| Error::Closed)?
    }
    pub async fn shutdown(&self) -> Result<(), Error> {
        self.ctrl(Control::Shutdown).await
    }
}
#[cfg(feature = "rtc")]
pub struct LiveKitSink(pub livekit::webrtc::audio_source::native::NativeAudioSource);
#[cfg(feature = "rtc")]
impl AudioSink for LiveKitSink {
    fn capture(&self, pcm: Vec<i16>) -> Capture<'_> {
        Box::pin(async move {
            use livekit::webrtc::prelude::AudioFrame;
            let channels = self.0.num_channels();
            self.0
                .capture_frame(&AudioFrame {
                    samples_per_channel: pcm.len() as u32 / channels,
                    data: std::borrow::Cow::Owned(pcm),
                    sample_rate: self.0.sample_rate(),
                    num_channels: channels,
                })
                .await
                .map_err(|e| e.to_string())
        })
    }
    fn clear(&self) {
        self.0.clear_buffer();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Mutex,
    };
    #[derive(Default)]
    struct Sink {
        frames: Mutex<Vec<Vec<i16>>>,
        clears: AtomicUsize,
        blocked: AtomicBool,
        entered: tokio::sync::Notify,
        released: tokio::sync::Notify,
    }
    impl AudioSink for Sink {
        fn capture(&self, p: Vec<i16>) -> Capture<'_> {
            Box::pin(async move {
                self.entered.notify_one();
                if self.blocked.load(Ordering::SeqCst) {
                    self.released.notified().await;
                    return Ok(());
                }
                self.frames.lock().unwrap().push(p);
                Ok(())
            })
        }
        fn clear(&self) {
            self.clears.fetch_add(1, Ordering::SeqCst);
            self.released.notify_waiters();
        }
    }
    #[tokio::test(start_paused = true)]
    async fn ordered_frames_require_authorization_and_finish_waits_for_drain() {
        let sink = Arc::new(Sink::default());
        let p = Playout::spawn(sink.clone(), 24000, 1, 48000);
        p.begin(1).await.unwrap();
        assert_eq!(p.push(1, vec![1; 240]).await, Err(Error::Unauthorized));
        p.authorize(1).await.unwrap();
        p.push(1, vec![1; 240]).await.unwrap();
        p.push(1, vec![2; 240]).await.unwrap();
        assert_eq!(
            *sink.frames.lock().unwrap(),
            vec![vec![1; 240], vec![2; 240]]
        );
        let t = tokio::spawn({
            let p = p.clone();
            async move { p.finish(1).await }
        });
        tokio::task::yield_now().await;
        assert!(!t.is_finished());
        tokio::time::advance(Duration::from_millis(19)).await;
        assert!(!t.is_finished());
        tokio::time::advance(Duration::from_millis(1)).await;
        assert_eq!(t.await.unwrap(), Ok(Completion::LocalQueueDrained));
        assert_eq!(p.push(1, vec![3; 240]).await, Err(Error::Finished));
        p.shutdown().await.unwrap();
    }
    #[tokio::test]
    async fn cancel_preempts_blocked_capture_and_stale_attempt_cannot_escape() {
        let sink = Arc::new(Sink::default());
        sink.blocked.store(true, Ordering::SeqCst);
        let p = Playout::spawn(sink.clone(), 24000, 1, 48000);
        p.begin(1).await.unwrap();
        p.authorize(1).await.unwrap();
        let t = tokio::spawn({
            let p = p.clone();
            async move { p.push(1, vec![1; 240]).await }
        });
        sink.entered.notified().await;
        tokio::time::timeout(Duration::from_millis(100), p.cancel(1))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(t.await.unwrap(), Err(Error::Cancelled));
        assert!(sink.frames.lock().unwrap().is_empty());
        assert_eq!(p.push(1, vec![1; 240]).await, Err(Error::Stale));
        p.begin(2).await.unwrap();
        assert_eq!(p.authorize(1).await, Err(Error::Stale));
        assert_eq!(p.begin(1).await, Err(Error::Stale));
        p.shutdown().await.unwrap();
    }
    #[tokio::test(start_paused = true)]
    async fn cancellation_invalidates_completion_waiter() {
        let sink = Arc::new(Sink::default());
        let p = Playout::spawn(sink, 24000, 1, 48000);
        p.begin(1).await.unwrap();
        p.authorize(1).await.unwrap();
        p.push(1, vec![1; 24000]).await.unwrap();
        let end = tokio::spawn({
            let p = p.clone();
            async move { p.finish(1).await }
        });
        tokio::task::yield_now().await;
        p.begin(2).await.unwrap();
        assert_eq!(end.await.unwrap(), Err(Error::Cancelled));
        assert_eq!(p.finish(1).await, Err(Error::Stale));
        p.shutdown().await.unwrap();
    }
    #[tokio::test]
    async fn model_speech_gate_does_not_touch_input_or_allow_unauthorized_changes() {
        let sink = Arc::new(Sink::default());
        let p = Playout::spawn(sink.clone(), 24000, 1, 48000);
        p.begin(1).await.unwrap();
        assert_eq!(p.speech_decision(1, false).await, Err(Error::Unauthorized));
        p.authorize(1).await.unwrap();
        p.speech_decision(1, false).await.unwrap();
        let mut input = crate::core::Gate::new(48000);
        input.input(&[1; 160]);
        assert_eq!(input.input_samples(), 160);
        assert_eq!(p.push(1, vec![1; 240]).await, Err(Error::Unauthorized));
        p.speech_decision(1, true).await.unwrap();
        p.push(1, vec![1; 240]).await.unwrap();
        assert_eq!(sink.frames.lock().unwrap().len(), 1);
        p.shutdown().await.unwrap();
    }
    #[tokio::test]
    async fn malformed_frames_fail_without_advancing_queue() {
        let p = Playout::spawn(Arc::new(Sink::default()), 24000, 2, 480);
        p.begin(1).await.unwrap();
        p.authorize(1).await.unwrap();
        for frame in [vec![], vec![1], vec![1; 482]] {
            assert_eq!(p.push(1, frame).await, Err(Error::InvalidPcm));
        }
        assert_eq!(p.finish(1).await, Ok(Completion::LocalQueueDrained));
        p.shutdown().await.unwrap();
    }
}

#[cfg(test)]
mod callback_failure_tests {
    use super::*;
    struct Unresponsive {
        entered: tokio::sync::Notify,
    }
    impl AudioSink for Unresponsive {
        fn capture(&self, _pcm: Vec<i16>) -> Capture<'_> {
            Box::pin(async {
                self.entered.notify_one();
                std::future::pending().await
            })
        }
        fn clear(&self) {}
    }
    #[tokio::test(start_paused = true)]
    async fn unresponsive_native_callback_closes_adapter_before_reuse() {
        let sink = Arc::new(Unresponsive {
            entered: tokio::sync::Notify::new(),
        });
        let p = Playout::spawn(sink.clone(), 24000, 1, 24000);
        p.begin(1).await.unwrap();
        p.authorize(1).await.unwrap();
        let t = tokio::spawn({
            let p = p.clone();
            async move { p.push(1, vec![1; 24000]).await }
        });
        sink.entered.notified().await;
        p.cancel(1).await.unwrap();
        tokio::time::advance(Duration::from_millis(101)).await;
        assert_eq!(t.await.unwrap(), Err(Error::Cancelled));
        assert_eq!(p.begin(2).await, Err(Error::Closed));
    }
}

#[cfg(test)]
mod rvc_contract_tests {
    use super::*;
    use crate::core::Gate;
    use std::sync::Mutex;
    struct Sink(Mutex<Vec<i16>>);
    impl AudioSink for Sink {
        fn capture(&self, p: Vec<i16>) -> Capture<'_> {
            Box::pin(async move {
                self.0.lock().unwrap().extend(p);
                Ok(())
            })
        }
        fn clear(&self) {}
    }
    #[tokio::test(start_paused = true)]
    async fn speculative_gate_and_native_queue_preserve_rvc_release_contract() {
        let sink = Arc::new(Sink(Mutex::new(Vec::new())));
        let p = Playout::spawn(sink.clone(), 24000, 1, 24000);
        let mut g = Gate::new(24000);
        let a = g.start_speculation();
        p.begin(a).await.unwrap();
        g.push(a, vec![1; 120]).unwrap();
        g.push(a, vec![2; 360]).unwrap();
        assert_eq!(g.next_pcm(a), Ok(None));
        assert!(sink.0.lock().unwrap().is_empty());
        g.authorize_final(a).unwrap();
        p.authorize(a).await.unwrap();
        while let Some(pcm) = g.next_pcm(a).unwrap() {
            p.push(a, pcm).await.unwrap();
        }
        assert_eq!(
            *sink.0.lock().unwrap(),
            [vec![1; 120], vec![2; 360]].concat()
        );
        g.generation_end(a).unwrap();
        assert_eq!(p.finish(a).await, Ok(Completion::LocalQueueDrained));
        g.playback_complete(a).unwrap();
        let b = g.start_speculation();
        p.begin(b).await.unwrap();
        g.push(b, vec![3; 240]).unwrap();
        g.cancel();
        p.cancel(b).await.unwrap();
        assert_eq!(p.push(b, vec![3; 240]).await, Err(Error::Stale));
        assert_eq!(sink.0.lock().unwrap().len(), 480);
        p.shutdown().await.unwrap();
    }
    #[tokio::test(start_paused = true)]
    async fn final_partial_frame_preserves_every_sample_and_pads_only_silence() {
        let sink = Arc::new(Sink(Mutex::new(Vec::new())));
        let p = Playout::spawn(sink.clone(), 24000, 1, 24000);
        p.begin(1).await.unwrap();
        p.authorize(1).await.unwrap();
        p.push(1, vec![7; 241]).await.unwrap();
        assert_eq!(p.finish(1).await, Ok(Completion::LocalQueueDrained));
        let out = sink.0.lock().unwrap().clone();
        assert_eq!(out.len(), 480);
        assert_eq!(&out[..241], &[7; 241]);
        assert!(out[241..].iter().all(|v| *v == 0));
        p.shutdown().await.unwrap();
    }
}
