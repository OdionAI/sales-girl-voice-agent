use std::collections::VecDeque;

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    Stale,
    InvalidPcm,
    Capacity,
    Unsupported,
    NotReady,
}

#[derive(Default)]
struct Attempt {
    id: u64,
    authorized: bool,
    ended: bool,
    held: VecDeque<Vec<i16>>,
    samples: usize,
}

/// Single owner for ordered, generation-scoped PCM. A full queue fails the
/// attempt; it never discards valid chunks to make room for newer ones.
pub struct Gate {
    attempt: Option<Attempt>,
    next_id: u64,
    speech_enabled: bool,
    input_samples: u64,
    max_samples: usize,
}

impl Gate {
    pub fn new(max_samples: usize) -> Self {
        Self {
            attempt: None,
            next_id: 0,
            speech_enabled: true,
            input_samples: 0,
            max_samples,
        }
    }
    pub fn input(&mut self, pcm: &[i16]) {
        self.input_samples += pcm.len() as u64;
    }
    pub fn input_samples(&self) -> u64 {
        self.input_samples
    }
    pub fn start_speculation(&mut self) -> u64 {
        self.cancel();
        self.next_id += 1;
        self.attempt = Some(Attempt {
            id: self.next_id,
            ..Attempt::default()
        });
        self.next_id
    }
    pub fn cancel(&mut self) {
        self.attempt = None;
    }
    fn current(&mut self, id: u64) -> Result<&mut Attempt, Error> {
        self.attempt
            .as_mut()
            .filter(|a| a.id == id)
            .ok_or(Error::Stale)
    }
    /// Caller must establish authoritative final, turn ownership and hypothesis
    /// validity using the reference coordinator. This is deliberately not a
    /// transcript-matching shortcut.
    pub fn authorize_final(&mut self, id: u64) -> Result<(), Error> {
        self.current(id)?.authorized = true;
        Ok(())
    }
    pub fn push(&mut self, id: u64, pcm: Vec<i16>) -> Result<(), Error> {
        let max = self.max_samples;
        let a = self.current(id)?;
        if a.ended {
            return Err(Error::NotReady);
        }
        if pcm.is_empty() {
            return Err(Error::InvalidPcm);
        }
        if a.samples.saturating_add(pcm.len()) > max {
            self.cancel();
            return Err(Error::Capacity);
        }
        a.samples += pcm.len();
        a.held.push_back(pcm);
        Ok(())
    }
    /// Recheck identity, authority and speech gate immediately before playback.
    pub fn next_pcm(&mut self, id: u64) -> Result<Option<Vec<i16>>, Error> {
        let enabled = self.speech_enabled;
        let a = self.current(id)?;
        if !a.authorized || !enabled {
            return Ok(None);
        }
        let chunk = a.held.pop_front();
        if let Some(ref p) = chunk {
            a.samples -= p.len();
        }
        Ok(chunk)
    }
    /// Only an authorized model/tool decision may invoke this boundary.
    pub fn speech_decision(&mut self, id: u64, enabled: bool) -> Result<(), Error> {
        if !self.current(id)?.authorized {
            return Err(Error::NotReady);
        }
        self.speech_enabled = enabled;
        if !enabled {
            let a = self.current(id)?;
            a.held.clear();
            a.samples = 0;
        }
        Ok(())
    }
    pub fn generation_end(&mut self, id: u64) -> Result<(), Error> {
        self.current(id)?.ended = true;
        Ok(())
    }
    /// SDK/receiver completion is a separate event from provider generation end.
    pub fn playback_complete(&mut self, id: u64) -> Result<(), Error> {
        let a = self.current(id)?;
        if !a.ended || !a.authorized || !a.held.is_empty() {
            return Err(Error::NotReady);
        }
        self.attempt = None;
        Ok(())
    }
    pub fn tool(&mut self, _name: &str) -> Result<(), Error> {
        self.cancel();
        Err(Error::Unsupported)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn speculative_audio_requires_final_authority() {
        let mut g = Gate::new(100);
        let a = g.start_speculation();
        g.push(a, vec![1, 2]).unwrap();
        assert_eq!(g.next_pcm(a), Ok(None));
        assert_eq!(g.speech_decision(a, false), Err(Error::NotReady));
        g.authorize_final(a).unwrap();
        assert_eq!(g.next_pcm(a), Ok(Some(vec![1, 2])));
    }
    #[test]
    fn stale_callbacks_and_cancellation_cannot_escape() {
        let mut g = Gate::new(100);
        let a = g.start_speculation();
        g.push(a, vec![1]).unwrap();
        g.authorize_final(a).unwrap();
        let b = g.start_speculation();
        assert_eq!(g.next_pcm(a), Err(Error::Stale));
        assert_eq!(g.authorize_final(a), Err(Error::Stale));
        assert_eq!(g.push(a, vec![2]), Err(Error::Stale));
        g.cancel();
        assert_eq!(g.next_pcm(b), Err(Error::Stale));
    }
    #[test]
    fn ordered_chunks_and_playout_are_distinct_from_generation() {
        let mut g = Gate::new(100);
        let a = g.start_speculation();
        g.push(a, vec![1]).unwrap();
        g.push(a, vec![2]).unwrap();
        g.authorize_final(a).unwrap();
        g.generation_end(a).unwrap();
        assert_eq!(g.playback_complete(a), Err(Error::NotReady));
        assert_eq!(g.next_pcm(a), Ok(Some(vec![1])));
        assert_eq!(g.next_pcm(a), Ok(Some(vec![2])));
        g.playback_complete(a).unwrap();
        assert_eq!(g.next_pcm(a), Err(Error::Stale));
    }
    #[test]
    fn listening_suppresses_output_and_retains_input() {
        let mut g = Gate::new(100);
        let a = g.start_speculation();
        g.authorize_final(a).unwrap();
        g.push(a, vec![1]).unwrap();
        g.speech_decision(a, false).unwrap();
        g.input(&[7; 32]);
        assert_eq!(g.next_pcm(a), Ok(None));
        assert_eq!(g.input_samples(), 32);
    }
    #[test]
    fn capacity_and_unsupported_tools_fail_closed() {
        let mut g = Gate::new(1);
        let a = g.start_speculation();
        assert_eq!(g.push(a, vec![1, 2]), Err(Error::Capacity));
        assert_eq!(g.authorize_final(a), Err(Error::Stale));
        assert_eq!(g.tool("transfer_money"), Err(Error::Unsupported));
    }
}
