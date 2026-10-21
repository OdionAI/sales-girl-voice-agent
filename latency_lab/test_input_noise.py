import math,os,random,unittest
from array import array
from types import SimpleNamespace
from unittest.mock import Mock,patch
from .input_noise import InputNoiseFilter

class Tests(unittest.TestCase):
 def setUp(self):
  self.env=patch.dict(os.environ,{'RVC_INPUT_NOISE_SUPPRESSION':'1','RVC_INPUT_NOISE_AGENT_IDS':'wema'});self.env.start();self.addCleanup(self.env.stop)
  self.trace=Mock();self.ctx=SimpleNamespace(agent_id='wema')
 def test_native_apm_reduces_stationary_noise_preserves_duration(self):
  rng=random.Random(81);raw=array('h',[rng.randint(-1600,1600) for _ in range(16000*5)]).tobytes()
  f=InputNoiseFilter(self.ctx,self.trace);self.assertTrue(f.enabled)
  out=b''.join(f.process(raw[i:i+3200]) for i in range(0,len(raw),3200))+f.finish()
  self.assertEqual(len(out),len(raw))
  energy=lambda b:sum(x*x for x in array('h',b))
  ratio=energy(out[32000:])/energy(raw[32000:]);self.assertLess(ratio,.8)
  print('Native stationary-noise energy ratio:',round(ratio,4))
 def test_chunk_boundaries_match_whole_stream_and_tail_flush(self):
  raw=array('h',[int(2000*math.sin(i*.11)) for i in range(16403)]).tobytes()
  f=InputNoiseFilter(self.ctx,self.trace);whole=f.process(raw)+f.finish()
  g=InputNoiseFilter(self.ctx,self.trace);parts=b''.join(g.process(raw[i:i+146]) for i in range(0,len(raw),146))+g.finish()
  self.assertEqual(whole,parts);self.assertEqual(len(parts),len(raw));self.assertTrue(any(parts))
 def test_silence_stays_silent(self):
  f=InputNoiseFilter(self.ctx,self.trace);self.assertEqual(f.process(bytes(3200)),bytes(3200))
 def test_other_agents_and_disabled_are_exact_passthrough(self):
  pcm=b'abc'
  self.assertEqual(InputNoiseFilter(SimpleNamespace(agent_id='other'),self.trace).process(pcm),pcm)
  with patch.dict(os.environ,{'RVC_INPUT_NOISE_SUPPRESSION':'0'}):
   self.assertEqual(InputNoiseFilter(self.ctx,self.trace).process(pcm),pcm)
 def test_failure_preserves_audio_and_marks_disabled(self):
  f=InputNoiseFilter(self.ctx,self.trace);f.apm=Mock();f.apm.process_stream.side_effect=RuntimeError('failure')
  pcm=b'12'*173
  self.assertEqual(f.process(pcm),pcm);self.assertFalse(f.enabled);self.assertFalse(f.pending)
  self.assertTrue(any(c.args[0]=='input_noise_filter_error' for c in self.trace.record.call_args_list))
 def test_init_failure_is_explicit_passthrough(self):
  with patch('latency_lab.input_noise.rtc.AudioProcessingModule',side_effect=RuntimeError('unavailable')):
   f=InputNoiseFilter(self.ctx,self.trace)
  self.assertFalse(f.enabled);self.assertEqual(f.process(b'12'*160),b'12'*160)
  self.assertTrue(any(c.args[0]=='input_noise_filter_error' for c in self.trace.record.call_args_list))
