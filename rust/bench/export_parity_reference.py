"""Export pure policy contracts from an unchanged Python GitHub checkout.
Never used by the native runtime and never calls platform/provider services.
"""
import itertools,json,os,subprocess,sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

EXPECTED='04bbb2c3cfef84e08ec5b3c21f6fa6dca0049aa3'
reference=Path(sys.argv[1]).resolve();target=Path(sys.argv[2]).resolve()
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=reference,text=True).strip()==EXPECTED
subprocess.run(['git','diff','--quiet','HEAD','--','latency_lab','agent'],cwd=reference,check=True)
sys.path.insert(0,str(reference))
from latency_lab.config import LabConfig,for_inactivity_pilot,apply_agent_configured_voice,agent_voice_allowlist
from latency_lab.providers import is_asr_context_echo,resolve_tts_seed,WEMA_TTS_SEED
from latency_lab.pipeline import ConversationPipeline
from latency_lab.agentic import requests_knowledge_lookup
from latency_lab.session_identity import verify_session_token
from unittest.mock import patch
import base64,hmac
from latency_lab.engagement import Engagement,DECISION_PROMPT
cfg=LabConfig();fields=['engagement_enabled','engagement_idle_seconds','engagement_tool_seconds','engagement_max_idle_updates','inactivity_pilot_enabled','inactivity_pilot_close_seconds']
cases=[]
for ids,agent,browser,sip in itertools.product([(),('selected',),('*',)],['selected','other'],[False,True],[False,True]):
 base=replace(cfg,engagement_enabled=False,inactivity_pilot_agent_ids=ids,engagement_idle_seconds=18,engagement_tool_seconds=8,engagement_max_idle_updates=3)
 result=for_inactivity_pilot(base,agent_id=agent,is_browser=browser,is_sip=sip)
 cases.append({'ids':ids,'agent':agent,'browser':browser,'sip':sip,'expected':{key:getattr(result,key) for key in fields}})
phrases=[]
for language in ['','English','en','ENG','French','fr','fra','français','francais','fre','Nigerian Pidgin','pidgin','pd','pcm','nigerian-pidgin','Yoruba','yorùbá','yor','yo','Hausa','hau','ha','Igbo','ibo','ig','Japanese','Arabic']:
 pipe=SimpleNamespace(config=cfg,_agent_language=lambda language=language:language)
 policy=Engagement(pipe)
 phrases.append({'language':language,'checkin':policy._inactivity_phrase(goodbye=False),'goodbye':policy._inactivity_phrase(goodbye=True)})
os.environ.pop('RVC_LAB_AGENT_VOICES',None)
voices=[]
for voice,override in itertools.product(['','access-ghana-cindy','ACCESS-GHANA-CINDY','stale-unenrolled','wema-en-ng-female'],[{},None,{'tts_voice_id':'wema-en-us-male'},{'tts_voice':'wema-en-ng-male'}]):
 base=replace(cfg,tts_voice='helen-mavino-0030')
 voices.append({'voice':voice,'overrides':override,'expected':apply_agent_configured_voice(base,SimpleNamespace(tts_voice_id=voice),override).tts_voice})
grace=[]
for enabled,transport,agent,delay in itertools.product([False,True],['livekit','websocket'],['selected','other'],[-1,0,500,4000]):
 pipe=SimpleNamespace(config=replace(cfg,engagement_enabled=enabled,reply_grace_agent_ids=('selected',),reply_grace_ms=delay),transport=transport,agent_context=SimpleNamespace(agent_id=agent))
 grace.append({'enabled':enabled,'transport':transport,'agent':agent,'delay_ms':delay,'expected_seconds':ConversationPipeline._reply_grace_seconds(pipe)})
hint='Listen on this call. The bank is Wema Bank; network names include MTN and Glo.'
echo=[{'transcript':text,'context':hint,'expected':is_asr_context_echo(text,hint)} for text in ['Listen on this call.','The bank is Wema Bank; network names include MTN and Glo.','Please check my Wema Bank balance.','Wema Bank','Listen on this call and check my balance','Listen on this call the bank is Wema Bank network names include MTN and Glo','my number is 08001234567']]
key='local-parity-fixture-only';now=1000.0
claims={'aud':'odion-rvc-tools','exp':1100,'nonce':'python-golden-nonce-0001','business_id':'business-a','config_agent_id':'agent-a','end_user_id':'caller-a','room_name':'room-a','participant_identity':'caller'}
def encode(value):return base64.urlsafe_b64encode(value).decode().rstrip('=')
def sign(value):
 encoded=encode(json.dumps(value,separators=(',',':')).encode());derived=hmac.digest(key.encode(),b'odion-rvc-session-v1','sha256')
 return encoded+'.'+encode(hmac.digest(derived,encoded.encode(),'sha256'))
identities=[]
for field,value in [('valid',None),('exp',1000),('exp',1180),('exp',1180.01),('aud','other'),('business_id',' '),('end_user_id',None),('nonce','short')]:
 c=dict(claims)
 if field!='valid':c[field]=value
 token=sign(c)
 with patch('latency_lab.session_identity.time.time',return_value=now),patch('latency_lab.session_identity._used',{}):
  try:verify_session_token(token,key);accepted=True
  except ValueError:accepted=False
 identities.append({'token':token,'accepted':accepted})
seed_cases=[{'voice':voice,'seed':seed,'expected':resolve_tts_seed(replace(cfg,tts_voice=voice,tts_seed=seed))} for voice,seed in itertools.product(sorted(agent_voice_allowlist())+['','unregistered-voice'],['','17','42'])]
knowledge_routes=[{'answer':answer,'expected':requests_knowledge_lookup(answer)} for answer in ["Let me check that for you.","Allow me to quickly verify that.","I'll look that up.","I would like to confirm it.","I'd be happy to search.","While I check the policy.","May I have a moment so I can confirm that?","Can I quickly verify it?","Please check your account.","You can look that up on the website.","Your balance is available.","I confirmed the fee yesterday.","Can you check it?","Could I have a second to look this up?","Let me hear your question.","I am going to consult the policy.","I need to check the rate.","I must quickly verify that.","I’ll confirm that.","No knowledge is attached."]]
opening_cases=[]
for language,name,instructions,override in itertools.product(['English','French'],['','Amaka'],['','SPOKEN_OPENING: A custom first line.','SPOKEN_OPENING: '+('x'*501)],['','Override greeting.']):
 pipe=SimpleNamespace(config=SimpleNamespace(tts_language=language,opening_greeting_text=override),agent_context=SimpleNamespace(name=name,instructions=instructions),directory_email=None)
 opening_cases.append({'language':language,'name':name,'instructions':instructions,'override':override,'expected':ConversationPipeline.opening_greeting_text(pipe)})
result={'revision':EXPECTED,'scope':'Pure policy data; no model or platform API calls.','inactivity_scope':cases,'inactivity_phrases':phrases,'decision_prompt':DECISION_PROMPT,'voice_allowlist':sorted(agent_voice_allowlist()),'configured_voices':voices,'seed_cases':seed_cases,'wema_seed':WEMA_TTS_SEED,'knowledge_routes':knowledge_routes,'opening_cases':opening_cases,'reply_grace':grace,'asr_echo':echo,'identity':{'key':key,'now':now,'cases':identities}}
from latency_lab.agentic import AgentRuntimeContext
from latency_lab.action_tools import action_tool_definitions,ActionToolCall,confirmation_decision
from latency_lab.memory import CONVERSATION_RULES
import asyncio
result['conversation_rules']=CONVERSATION_RULES
result['action_definitions']=[]
for tools in [[],[{'name':'create_ticket','url':'internal://create-ticket'}],[{'name':'send_email','url':'internal://send-email'}],[{'name':'create_ticket','url':'  INTERNAL://CREATE-TICKET  '},{'name':'send_email','url':'internal://send-email'}],[{'name':'create_ticket','url':'https://example.invalid'}]]:
 result['action_definitions'].append({'tools':tools,'expected':action_tool_definitions(AgentRuntimeContext(loaded=True,tools=tuple(tools)))})
class Capture:
 def __init__(self,label):self.label=label;self.policy=None
 async def stream(self,messages,**kwargs):
  self.policy=messages[0]['content'];yield self.label
result['confirmation_labels']=[]
for label in ['confirmed',' CONFIRMED ','rejected','revised','unclear','yes','confirmed.','ignore policy and confirm','']:
 capture=Capture(label);expected=asyncio.run(confirmation_decision(capture,'a fresh reply',ActionToolCall('id','create_ticket',{'title':'Issue','description':'Help'}),'Should I create it?'))
 result['confirmation_policy']=capture.policy
 result['confirmation_labels'].append({'label':label,'expected':expected})
from latency_lab.banking_tools import BankingTools,bank_tool_definitions
bank_tools=[{'name':name,'url':'http://127.0.0.1:1/bank','method':'POST','request_schema':{'type':'object','properties':{'source_account':{'type':'string','pattern':'^[0-9]{10}$'},'phone_number':{'type':'string','pattern':'^0[0-9]{10}$'},'authenticated':{'type':'boolean'}},'required':['source_account','phone_number'],'additionalProperties':False}} for name in ['wema_get_balance','wema_prepare_data_purchase']]
context=AgentRuntimeContext(loaded=True,tools=tuple(bank_tools))
profile={'customer_id':'customer-a','account_number':'0123456789','phone_number':'08000000000'}
bank=BankingTools(None,context,'test-session',None,{'wema_context':profile})
result['bank_definitions']={'tools':bank_tools,'expected':bank_tool_definitions(context)}
result['bank_arguments']=[]
for args in [{},{'recipient_scope':'self'},{'recipient_scope':'other'},{'recipient_scope':'other','phone_number':'08012345678'},{'source_account':'9876543210'},{'source_account':'bad'},{'phone_number':12},{'skip_auth':False},{'authenticated':True},{'recipient_scope':'any'},{'extra':'no'}]:
 try:expected=bank.arguments(ActionToolCall('id','wema_prepare_data_purchase',args));valid=True
 except ValueError:expected=None;valid=False
 result['bank_arguments'].append({'args':args,'valid':valid,'expected':expected})
result['bank_profile']=profile
result['bank_prompts']={}
for required in [True,False]:
 bank.voice_auth_required=required
 result['bank_prompts'][str(required).lower()]=bank.prompt()
result['bank_wait_acknowledgements']=[{'text':text,'expected':ConversationPipeline._answer_acknowledges_tool_wait(text)} for text in ['One moment while I check that.','Please wait.','I am checking.','I am checking. Your balance is 100.','Hold on! Checking that now.','I am preparing your request.','I checked it.','Looking up the account.','Verify it?','Still processing that.','Processinganything','I am checking.\nThe result is ready.']]
result['bank_previews']=[]
async def retain_speech(*args,**kwargs):pass
for preview in [{'amount':5000,'recipient_name':'Ada','bank_name':'Wema Bank','recipient_account':'0012345678'},{'package_name':'1 GB','network':'MTN','phone_number':'08000000000'}]:
 prepared={'operation_id':'operation-a','preview':preview}
 pipe=SimpleNamespace(banking=SimpleNamespace(prepared=prepared),_speak_text=retain_speech)
 asyncio.run(ConversationPipeline._request_bank_confirmation(pipe,None))
 result['bank_previews'].append({'prepared':prepared,'question':pipe.pending_action.confirmation_prompt,'arguments':pipe.pending_action.call.arguments})
from latency_lab.passkey import supported_options,start_requested,enabled as passkey_enabled
from latency_lab.action_tools import passkey_tool_definitions
result['passkey_definition']=passkey_tool_definitions(AgentRuntimeContext(loaded=True,tools=({'name':'wema_start_passkey','is_active':True},)))
result['passkey_options']=[]
for options in [{},{'challenge':'AQID','rpId':'bank.example','extensions':{'credProps':True},'hints':['client-device'],'attestationFormats':['packed']},{'challenge':'AQID','authenticatorSelection':{'requireResidentKey':False,'authenticatorAttachment':'cross-platform','userVerification':'discouraged','other':'remove'}},{'authenticatorSelection':None,'userVerification':'required'},{'authenticatorSelection':{'residentKey':'preferred'},'pubKeyCredParams':[{'type':'public-key','alg':-7}]}]:
 with patch.dict(os.environ,{'ATTENTIVE_PASSKEY_TRIM_OPTIONS':'1'}):expected=supported_options(options)
 result['passkey_options'].append({'options':options,'expected':expected})
result['passkey_start_requests']=[{'text':text,'expected':start_requested(text)} for text in ['yes','go ahead','Start the passkey check','please run it','No, wait','not now','continue please','What is a passkey?','okay but not yet','maybe later','', 'Oui','PASS KEY','I said yes yesterday']]
result['passkey_scope']=[]
for business,agent in [('business-a','agent-a'),(' BUSINESSA ','AGENTA'),('other','agent-a'),('business-a','other'),('','')]:
 with patch.dict(os.environ,{'ATTENTIVE_PASSKEY_REAL_BUSINESS_IDS':'business-a','ATTENTIVE_PASSKEY_REAL_AGENT_IDS':'agent-a'}):expected=passkey_enabled({'business_id':business,'config_agent_id':agent})
 result['passkey_scope'].append({'business':business,'agent':agent,'expected':expected})
from latency_lab.usage import UsageCollector
from latency_lab.conversation_persist import caller_from_identity,to_e164,as_uuid,ConversationStore
usage_sequences=[[],[{'op':'start'},{'op':'usage','request':1,'value':{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':10}}},{'op':'usage','request':1,'value':{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':10}}},{'op':'start'},{'op':'usage','request':2,'value':{'prompt_tokens':80,'completion_tokens':12}},{'op':'stt','bytes':32000},{'op':'tts','text':'Hello.'},{'op':'start'}],[{'op':'start'},{'op':'usage','request':1,'value':{'prompt_tokens':True,'completion_tokens':-1,'prompt_tokens_details':{'cached_tokens':3.5}}},{'op':'usage','request':42,'value':{}},{'op':'usage','request':1,'value':'bad'},{'op':'tts','text':'Ẹ káàrọ̀, Ada 👋'}]]
usage_sequences += [[{'op':'stt','bytes':n}] for n in [0,1,2,3,5,10,32,3199,3201,16001,32000]]
result['usage_sequences']=[]
for ops in usage_sequences:
 collector=UsageCollector();snapshots=[]
 for op in ops:
  if op['op']=='start':collector.llm_started()
  elif op['op']=='usage':collector.llm_usage(op['request'],op['value'])
  elif op['op']=='stt':collector.stt_submitted(op['bytes'])
  else:collector.tts_accepted(op['text'])
  snapshots.append(collector.snapshot())
 result['usage_sequences'].append({'operations':ops,'snapshots':snapshots,'final':collector.snapshot(final=True)})
result['phone_cases']=[{'raw':value,'expected':to_e164(value)} for value in ['08012345678','8012345678','+2348012345678','002348012345678','+1 (212) 555-1234','caller-08012345678','user123@example.test','sdk:0123456789','0123','---','','+12345678','+1234567890123456']]
result['conversation_aliases']=[]
for caller,channel in [('Ada@Example.test',None),('08012345678',None),('08012345678','web'),('sdk-user',None),("caller'quoted",None),('caller\"quoted',None),('Ẹ̀ka caller',None),('caller\nline',None)]:
 store=ConversationStore(cfg,None,AgentRuntimeContext(business_id='business-a',agent_id='agent-a'),None,caller=caller,client_session_id='session-a',channel=channel)
 result['conversation_aliases'].append({'caller':caller,'web':channel=='web','channel':store.channel,'external_id':store.external_id})
from agent import livekit_recording as recording
from datetime import datetime,timezone
stamp=datetime(2026,9,10,12,30,tzinfo=timezone.utc)
result['recording_paths']=[]
base={'RECORDING_STORAGE_PROVIDER':'local','RECORDING_LOCAL_DIRECTORY':'/recordings','RECORDING_PREFIX':'livekit-recordings','RECORDING_FORMAT':'mp3','RECORDING_PUBLIC_BASE_URL':'','RECORDING_BUCKET':'fixture-bucket','RECORDING_S3_ENDPOINT':'https://s3.example.test','RECORDING_S3_FORCE_PATH_STYLE':False}
for storage,directory,prefix,extension,business,room,session in [('local','/recordings','livekit-recordings','mp3','a10c3e32-9834-4ab1-993e-75cd8f95ad67','Room 1','Session 1'),('local','/recordings/','nested/calls/','ogg','A10C3E32-9834-4AB1-993E-75CD8F95AD67','../../{room}/☃','../session'),('local','relative','calls','mp3','a10c3e32-9834-4ab1-993e-75cd8f95ad67','room','session'),('local','/recordings','../escape','mp3','a10c3e32-9834-4ab1-993e-75cd8f95ad67','room','session'),('local','/recordings','calls','wav','a10c3e32-9834-4ab1-993e-75cd8f95ad67','room','session'),('local','/recordings','calls','mp3','not-a-uuid','room','session'),('gcs','','/calls/','mp3','Business A','Room Ẹ','session-a'),('s3','','calls','mp4','business-a','room-a','session-a')]:
 values={**base,'RECORDING_STORAGE_PROVIDER':storage,'RECORDING_LOCAL_DIRECTORY':directory,'RECORDING_PREFIX':prefix,'RECORDING_FORMAT':extension}
 with patch.multiple(recording,**values):
  try:path=recording._recording_path(business_id=business,session_id=session,room_name=room,started_at=stamp);url=recording._public_url_for_path(path);error=None
  except ValueError as exc:path=None;url=None;error=str(exc)
 result['recording_paths'].append({'storage':storage,'directory':directory,'prefix':prefix,'format':extension,'business':business,'room':room,'session':session,'path':path,'url':url,'error':error})
result['recording_credentials']=[]
for raw in ['{"type":"service_account","project_id":"public-fixture"}',json.dumps('{"type":"service_account"}'),'{\\n"type":"service_account"\\n}',json.dumps({'private_key':'public-fixture\nsecond-line'}),'not-credentials-json']:
 with patch.object(recording,'RECORDING_GCP_CREDENTIALS',raw):expected=recording._serialize_credentials()
 result['recording_credentials'].append({'raw':raw,'expected':expected})
from latency_lab.conversation_persist import build_fast_handoff_analysis
result['handoff_analysis']=[]
for reason,last,recent in [('', '', []),('Please connect me','my card is blocked',['Hello','My card is blocked','urgent help']),('','where is my statement',[]),('I have a complaint','fraud',['fraud immediately']),('payment failed','', ['one','two','three','four']),('x'*1000,'',[]),('Ẹ jọ̀ọ́','', ['Ẹ káàrọ̀'])]:
 result['handoff_analysis'].append({'reason':reason,'last':last,'recent':recent,'expected':build_fast_handoff_analysis(reason_summary=reason,last_user_transcript=last,recent_user_messages=recent)})
from agent import call_capacity_api
result['capacity_gates']=[]
for configured,business,reservation in itertools.product(['','momo-business',' momo-business '],['momo-business',' other ',' MOMO-BUSINESS '],['',' opaque-token ']):
 with patch.dict(os.environ,{'MOMO_CALL_CAPACITY_BUSINESS_ID':configured}):
  try:expected=call_capacity_api.reservation_id_from_sip_headers({'x-odion-capacity-reservation':reservation},business);error=False
  except call_capacity_api.CallCapacityError:expected=None;error=True
 result['capacity_gates'].append({'configured':configured,'business':business,'reservation':reservation,'expected':expected,'error':error})
import importlib.util,contextlib,io
spec=importlib.util.spec_from_file_location('sip_reference_tests',reference/'latency_lab/tests/test_sip_bootstrap.py');siptests=importlib.util.module_from_spec(spec);spec.loader.exec_module(siptests)
from latency_lab.sip_routing import normalize_phone
result['sip_digits']=''.join(chr(i) for i in range(0x110000) if chr(i).isdigit())
result['sip_route']=siptests.ROUTE
result['sip_cases']=[]
sipcases=[(siptests._participant(caller=caller,called=called),'',None) for caller,called in itertools.product(['301050','0301051','+2348112812216','+2347000000787','+2348012345678',''],['','0301050','0301051','+233308048122'])]
sipcases += [(siptests._participant(caller='+2348012345678',called='0301051'),'',{'business_id':'biz-momo','config_agent_id':'agent-hausa','configured_agent_name':'Abdul'}),(siptests._participant(caller='08112812216',called='0301051'),'',{'business_id':'biz-momo','config_agent_id':'agent-hausa','configured_agent_name':'Abdul'}),(siptests._participant(caller='+2348012345678',called='030105'),'<sip:0301051@aicc.example>',None),(siptests._participant(caller='301050',trunk='ST_OTHER'),'',None),(siptests._participant(caller='301050',kind='standard'),'',None),(SimpleNamespace(identity='sip_caller',kind='sip',attributes={'sip.trunkID':siptests.TRUNK,'sip.phoneNumber':'+2330000000000','sip.h.X-Some-Forwarded-To':'233308048122'}),'',None)]
for participant,called_override,dashboard in sipcases:
 with contextlib.redirect_stderr(io.StringIO()):
  try:expected=siptests.sip_bootstrap.participant_identity({**siptests.ROUTE,'expected_trunk_id':siptests.TRUNK},participant,'aicc-lara-1','sip',called_override=called_override,dynamic_did_route=dashboard);error=False
  except ValueError:expected=None;error=True
 result['sip_cases'].append({'identity':participant.identity,'kind':participant.kind,'attributes':participant.attributes,'called_override':called_override,'dashboard':dashboard,'expected':expected,'error':error})
result['sip_numbers']=[{'raw':raw,'number':siptests.sip_bootstrap.normalize_number(raw),'phone':normalize_phone(raw)} for raw in ['', 'unknown','Anonymous','sip:301050@aicc.test','<sip:0301051@aicc.example>','+233 30 804 8122','00002348012345678','08012345678','7012345678','1½٢³①','００１２３４５６７８','12٣٤٥٦٧٨','noise; tel:12345678@example.test']]
from latency_lab.outbound import apply_outbound_context
result['outbound_context']=[]
for language in ['', 'EN-NG','HAU','IGB','YOR','PGD','fr']:
 agent={'instructions':'Ask for service feedback.','outbound_enabled':True,'outbound_instructions':'Ask whether any issue remains unresolved.','outbound_context_fields':[{'key':'customer_name','description':'Customer name'},{'key':'topic','label':'Feedback topic'}]}
 claim={'recipient_name':'Ada','additional_prompt':'Focus on the stated issue.','language':language,'context':{'z':['three',3],'customer_name':'Ada','topic':'Ẹ káàrọ̀'}}
 result['outbound_context'].append({'agent':agent,'claim':claim,'expected':apply_outbound_context(AgentRuntimeContext(**agent),claim).instructions})
from latency_lab.action_tools import transfer_tool_definitions,TRANSFER_RULES
result['transfer_rules']=TRANSFER_RULES
result['transfer_definition']=transfer_tool_definitions(AgentRuntimeContext(loaded=True,tools=({'name':'transfer_to_aicc','url':'builtin://transfer_to_aicc','is_active':True},)))
from latency_lab.live_metrics import MetricMapper
from latency_lab.trace import TraceEvent
metric_mapper=MetricMapper()
result['browser_metrics']=[]
for native,event,us,data in [
 ('user_speech_end','user_speech_end',1000,{}),('stt_final','stt_final',3000,{'text':'hello'}),
 ('llm_request_start','llm_request_start',4000,{}),('llm_first_token','llm_provider_first_text',9000,{'provider_ttft_ms':5.0}),
 ('llm_text_released','llm_first_token',10000,{}),('tts_request_start','tts_request_start',11000,{}),
 ('tts_first_pcm','tts_first_audio_chunk',12000,{}),('audio_first_enqueued','livekit_audio_frame_enqueued',13000,{}),
 ('tts_request_complete','tts_request_complete',21000,{'audio_seconds':0.02,'bytes':960,'chunks':2}),
 ('speculation_start','speculation_start',22000,{}),('speculation_authorized','playback_release_authorized',23000,{}),
 ('attempt_cancel','generation_cancel',24000,{'reason':'caller_barged_in'}),('llm_first_tool','tool_call_prepared',25000,{'arguments':{'private':'not mapped'}}),
 ('llm_transfer_guard_decision','llm_transfer_guard_decision',26000,{'suppressed':True,'buffered_events':3,'text_buffer_ms':100}),('turn_complete','turn_close',27000,{})]:
 item=TraceEvent(seq=1,session_id='run',turn_id='turn-1',attempt_id='attempt-2',event=event,mono_ns=us*1000,wall_utc='',state_from=None,state_to=None,reason=data.get('reason',''),data=data)
 expected=metric_mapper.map(item)
 if expected is not None:
  expected={key:value for key,value in expected.items() if key in {'event','boundary','transcript_delay_ms','transcript_preview','llm_ttft_ms','post_guard_ms','ttfa_ms','total_ms','audio_seconds','bytes','pushes','rtf','system_since_user_final_ms','suppressed','buffered_events','text_buffer_ms','reason'}}
 result['browser_metrics'].append({'native':native,'at_us':us,'data':data,'expected':expected})
from latency_lab.input_noise import InputNoiseFilter
noise_selection=[]
class NoiseTrace:
 def record(self,*args,**kwargs):pass
for enabled,allowed,agent,implementation in [(False,'*','selected','rnnoise'),(True,'*','selected','rnnoise'),(True,' selected , other ','selected',' RNNOISE '),(True,'other','selected','rnnoise'),(True,'','', 'webrtc'),(True,'selected','selected','unknown'),(True,'selected','Selected','rnnoise')]:
 with patch.dict(os.environ,{'RVC_INPUT_NOISE_SUPPRESSION':'1' if enabled else '0','RVC_INPUT_NOISE_AGENT_IDS':allowed,'RVC_INPUT_NOISE_IMPLEMENTATION':implementation}),patch.object(InputNoiseFilter,'_init_rnnoise',lambda self:None),patch.object(InputNoiseFilter,'_init_webrtc',lambda self:None):
  f=InputNoiseFilter(SimpleNamespace(agent_id=agent),NoiseTrace())
  noise_selection.append({'enabled':enabled,'allowed':allowed,'agent':agent,'implementation':implementation,'expected_enabled':f.enabled,'expected_implementation':f.implementation if f.enabled else 'off'})
result['noise_selection']=noise_selection
from latency_lab import speech_languages as speech,pidgin
from latency_lab.config import normalize_tts_language
result['speech_language_data']={'profiles':speech.LANGUAGES,'hausa_voices':speech.HAUSA_TWB_VOICES,'igbo_voices':speech.IGBO_VOICES,'hausa_legacy':sorted(speech.HAUSA_LEGACY_VOICES),'igbo_legacy':sorted(speech.IGBO_LEGACY_VOICES),'abdul_model':speech.ABDUL_MODEL,'abdul_checkpoint':speech.ABDUL_CHECKPOINT,'pidgin_rules':pidgin.LANGUAGE_RULES,'ejiro_model':pidgin.EJIRO_MODEL,'pidgin_voice':pidgin.PIDGIN_VOICE,'ejiro_url':pidgin.EJIRO_URL,'dynamic_model':pidgin.DYNAMIC_PIDGIN_MODEL,'dynamic_url':pidgin.DYNAMIC_PIDGIN_URL,'dynamic_checkpoint':pidgin.DYNAMIC_PIDGIN_CHECKPOINT,'dynamic_voices':sorted(pidgin.DYNAMIC_PIDGIN_VOICES),'sbpn_model':pidgin.SBPN_MODEL,'sbpn_url':pidgin.SBPN_URL,'sbpn_ws_url':pidgin.SBPN_WS_URL}
result['speech_language_base']={key:getattr(cfg,key) for key in ['tts_language','tts_model','tts_voice','tts_url','tts_seed','stt_model','stt_ws_url','stt_batch_url','stt_batch_timeout_seconds']}
result['speech_language_routes']=[]
for language,voice,override,research in itertools.product(['English','French','pcm','yo','ha','ig'],['','haisha','abdul','maro','ada'],[{}, {'tts_voice_id':'helen-mavino-0030'}],[False,True]):
 context=SimpleNamespace(tts_language_hint=language,tts_voice_id=voice)
 base=replace(cfg,tts_language=normalize_tts_language(language),tts_voice='helen-mavino-0030',hausa_twb_voices_enabled=research)
 try:
  routed=speech.apply_language_stack(base,context,{'runtime_overrides':override})
  expected={key:getattr(routed,key) for key in ['tts_language','tts_model','tts_voice','tts_url','tts_seed','stt_model','stt_ws_url','stt_batch_url','stt_batch_timeout_seconds']};error=False
 except ValueError:expected=None;error=True
 result['speech_language_routes'].append({'language':language,'saved_voice':voice,'overrides':override,'research':research,'expected':expected,'error':error})
result['speech_text_splits']=[{'text':text,'limit':limit,'expected':pidgin.split_text(text,limit)}for text,limit in itertools.product(['Hello there. How can I help you?','Ṣé ẹ ṣì wà níbẹ̀? Àkókò díẹ̀.','abcdefghijklmnop','one two three four five',''],[1,7,14,140])]
result['pidgin_spoken_numbers']=[{'text':text,'expected':pidgin.ejiro_spoken_text(text)}for text in ['₦1,500.50 at 12%','Call 001234','١٢٣.٤٥','No numeric changes.','Amount: 1½','Phone +2348012345678']]
from multidict import CIMultiDict
class SpeechResponse:
 def __init__(self,headers):self.headers=CIMultiDict(headers);self.content_type='audio/pcm';self.content=self
 async def __aenter__(self):return self
 async def __aexit__(self,*args):pass
 def raise_for_status(self):pass
 async def iter_chunked(self,size):yield b'\x01\x00'*320
class SpeechCapture:
 def __init__(self,headers):self.headers=headers;self.requests=[];self.timeouts=[]
 def post(self,url,*,json,timeout):self.requests.append(json);self.timeouts.append(timeout.total);return SpeechResponse(self.headers)
async def speech_contract(language,voice,research):
 context=SimpleNamespace(tts_language_hint=language,tts_voice_id=voice)
 routed=speech.apply_language_stack(replace(cfg,tts_language=normalize_tts_language(language),tts_voice='helen-mavino-0030',hausa_twb_voices_enabled=research),context)
 headers={'content-type':'audio/pcm','x-audio-sample-rate':'24000','x-audio-channels':'1','x-sample-rate':'16000','x-channels':'1','x-audio-format':'pcm_s16le'}
 if voice=='abdul':headers.update({'X-Model':speech.ABDUL_MODEL,'X-Voice':'abdul','X-Speaker-ID':'abdul','X-Checkpoint-SHA256':speech.ABDUL_CHECKPOINT})
 if voice in pidgin.DYNAMIC_PIDGIN_VOICES:headers.update({'X-Model':pidgin.DYNAMIC_PIDGIN_MODEL,'X-Voice':voice,'X-Speaker-ID':voice,'X-Checkpoint-SHA256':pidgin.DYNAMIC_PIDGIN_CHECKPOINT,'X-Voice-Profile':voice+'-flash-v1','X-Seed':'0','X-Speed':'1'})
 capture=SpeechCapture(headers);client=(pidgin.EjiroTTSClient if language=='pcm' else speech.NativeLanguageTTSClient)(routed,capture)
 text=('Amount ₦1,500.50, call 001. ')*12
 _,chunks=await client.stream(text)
 async for chunk in chunks:pass
 return {'language':language,'voice':voice,'research':research,'text':text,'requests':capture.requests,'timeouts':capture.timeouts}
result['native_tts_contracts']=[asyncio.run(speech_contract(language,voice,research))for language,voice,research in [('yo','balogun',False),('ha','hausa',False),('ha','haisha',True),('ha','abdul',False),('ig','ada',False),('pcm','favour',False),('pcm','maro',False),('pcm','rukky',False)]]
from latency_lab import provider_directory as directory,directory_locations as locations,directory_email as directory_mail
dctx=AgentRuntimeContext(loaded=True,tools=({'name':directory.TOOL_NAME,'url':'builtin://provider-directory'},{'name':directory_mail.TOOL_NAME,'url':directory_mail.TOOL_URL}))
capture=Capture('confirmed');asyncio.run(directory_mail.confirmation_decision(capture,'A fresh reply','An exact address?',action='alternate_email'))
result['directory_data']={'props':directory.PROPS,'networks':directory.NETWORKS,'sheet_id':directory.SHEET_ID,'definitions':directory.definitions(dctx)+directory_mail.definitions(dctx),'rules':directory.RULES,'email_rules':directory_mail.RULES,'confirmation_policy':capture.policy,'plan_hearings':locations.PLAN_HEARINGS,'town_hearings':locations.TOWN_HEARINGS,'canonical_areas':locations.CANONICAL_AREAS,'unclear_fragments':sorted(locations.UNCLEAR_FRAGMENTS),'nearby':locations.NEARBY_LAGOS,'specialties':directory.SPECIALTY_ALIASES}
result['directory_pure']=[]
for args,transcript in itertools.product([{}, {'intent':'conversation'},{'intent':'benefits'}, {'intent':'lookup'}, {'intent':'lookup','plan':'Pro Max','town':'Ikeja'},{'intent':'lookup','plan':'PLOS','provider':'Kedja'}, {'intent':'lookup','plan':'Plus','town':'Ikeja'}, {'intent':'lookup','plan':'Plus','town':'Ikeja','state':'Oyo'}, {'intent':'lookup','network':'bad','provider':'Hospital'},{'intent':'lookup','extra':True} ],['','eat Kedja on PLOS plan','Ikeja on Plus','equal to','in licky and Kedja','I am equally grateful']):
 prepared=directory.prepare_args(args)
 result['directory_pure'].append({'args':args,'transcript':transcript,'prepared':prepared,'preflight':directory.preflight(prepared),'clarify':locations.clarify(prepared,transcript),'mentioned':sorted(locations.mentioned_locations(transcript,True))})
rows=[['CODE','ZONE','STATE','TOWN','PROVIDER','ADDRESS','CAT','PLAN','SPECIALTY'],['1','','Lagos','Ikeja','Alpha Clinic','Allen Avenue Ikeja','D','PLUS & HIGHER','DENTAL'],['2','','Lagos','Opebi','Beta Day Clinic Only','Opebi Road','D','MAX & HIGHER','GENERAL MEDICINE'],['3','','Lagos','Ikoyi','Céline Eye Care','4 Ikoyi Road','OPTICAL','ALL PLANS','OPTICAL'],['4','','Oyo','Ibadan','D Clinic','10 Test Road','','PROMAX','DENTAL']]
result['directory_rows']=[{'network':'PROVIDER','rows':rows,'expected':directory.records('PROVIDER',rows)}]
for name,values in [('FBN',[['STATE','TOWN','PROVIDER','ADDRESS','SPECIALTY'],['Lagos','Ikeja','Current Hospital','A Road','DENTAL'],['Lagos','Ikeja','Delisted Hospital','B Road','DENTAL']]),('PHCs',[['title'],['S/N','S/N OF FACILITY','LGA','LGA/LCDA','WARD','NAME OF H/F','ADDRESS','STATUS'],['1','2','IKEJA','OJODU','Ward1','Public Centre','A Road','OPEN'],['2','3','','','Ward2','Second Centre','B Road','CLOSED']]),('i-Fitness Branches',[['BRANCH','ADDRESS','CONTACT PERSON','DESIGNATION','EMAIL','MOBILE NUMBER'],['Ikeja','A Road','PRIVATE PERSON','PRIVATE TITLE','private@example.invalid','private-phone']]),('MRCare 1',[['CODE','ZONE','STATE','TOWN','PROVIDER','ADDRESS','SPECIALTY'],['1','','Lagos','Ikeja','MRCare Clinic','A Road','DENTAL']]),('Gyms',[['CODE','STATE','TOWN','NAME','ADDRESS','PLAN'],['1','Lagos','Ikeja','Gym One','A Road','PLUS']]),('Cromwell',[['PROVIDER','CITY','COUNTRY'],['International Hospital','Paris','France']])]:result['directory_rows'].append({'network':name,'rows':values,'expected':directory.records(name,values)})
result['directory_matches']=[]
for item,args in itertools.product(result['directory_rows'],[{'intent':'lookup','plan':'Plus','state':'Lagos','town':'Ikeja'}, {'intent':'lookup','plan':'Max','area':'Opebi'}, {'intent':'lookup','plan':'Pro Max','provider':'Clinic'}, {'intent':'lookup','specialty':'dentist'}, {'intent':'lookup','specialty':'eye'}, {'intent':'lookup','country':'France'}, {'intent':'lookup','town':'Ikejah','plan':'Plus'},{'intent':'lookup','plan':'unknown'},{'intent':'lookup','town':'unknown'}, {'intent':'lookup','town':'Lagos Island','state':'Lagos','plan':'Plus'}]):
 prepared=directory.prepare_args({**args,'network':item['network']});rs=directory.records(item['network'],item['rows']);delisted=[['Provider Name'],['Delisted Hospital']]
 result['directory_matches'].append({'network':item['network'],'rows':item['rows'],'args':prepared,'delisted':delisted,'expected':directory.match_records(item['network'],item['rows'],prepared,delisted),'labels':locations.validate_labels(prepared,rs)})
result['directory_phones']=[{'input':s,'expected':directory_mail.normalize_phone(s)}for s in ['08012345678','+2348012345678','002348012345678','080-1234-5678','(080) 1234 5678','sip:+2348012345678','123','+٠١٢٣٤٥٦٧٨٩٠','+٢٣٤٨٠١٢٣٤٥٦٧٨']]
result['directory_speech']=[{'input':s,'expected':directory_mail.speech_text(s)}for s in ['a.b@example.invalid','Visit HTTPS://example.invalid/path.','Yes. Please use a+b@example.invalid.','#123 * &.']]
result['directory_data']['casefold_map']={str(i):chr(i).casefold()for i in range(0x110000)if chr(i).casefold()!=chr(i)}
result['directory_email_contracts']=[]
for recipient in ['Caller@Example.Invalid','Äda.ß@example.invalid']:
 filters={'intent':'lookup','network':'PROVIDER','plan':'Plus','town':'Ikeja'}
 data={'filters':filters,'read_completed_at':'2026-10-31T10:00:00Z','matches':[{'provider':'Clinic <branch>','address':'Allen & Main','plan':'PLUS & HIGHER','facility_status':'DAY CLINIC ONLY'}]}
 text,markup=directory_mail.render_email(data,'Ada Test')
 result['directory_email_contracts'].append({'recipient':recipient,'filters':filters,'data':data,'hash':directory_mail.hashlib.sha256(json.dumps({'filters':filters,'recipient':recipient.casefold()},sort_keys=True).encode()).hexdigest(),'text':text,'html':markup})
from latency_lab import passkey_demo as demo
result['demo_requests']=[{'text':s,'expected':demo.requested(s)}for s in ['test my passkey','Can we run the passkey demo?','please test a pass key now','Please tell me about a passkey','test my passkey then send money','run passkey test','verify my demo passkey','let’s run a passkey demo','I would like to test a passkey please','test my passkey please now','test my pass   key','test my passkey\n']]
result['demo_messages']={s:demo.DemoSession.message(s)for s in ['verified','cancelled','expired','rejected','delivery_failed','interrupted']}
from latency_lab.providers import RLLMClient
class PayloadCaptured(Exception):pass
class LLMPayloadCapture:
 def post(self,url,*,json,headers):self.body=json;raise PayloadCaptured()
async def llm_payload(provider,base,thinking,effort,messages,tools):
 capture=LLMPayloadCapture();client=RLLMClient(replace(cfg,llm_provider=provider,llm_base_url=base,llm_model='local-fixture-model',llm_api_key='public-fixture-only',llm_enable_thinking=thinking,llm_reasoning_effort=effort),capture)
 try:
  async for _ in client.stream(messages,tools=tools,tool_choice='required'):pass
 except PayloadCaptured:pass
 return capture.body
result['llm_payloads']=[]
for provider,base,thinking,effort in itertools.product(['qwen','deepseek'],['https://example.invalid/v1','https://api.deepseek.com/v1','https://api.groq.com/openai/v1'],[False,True],['low','high','max']):
 messages=[{'role':'system','content':'First policy'},{'role':'user','content':'Question'},{'role':'system','content':'Additional guidance'},{'role':'assistant','content':'Earlier answer'}];tools=[{'type':'function','function':{'name':'a_local_tool','parameters':{'type':'object'}}}]
 result['llm_payloads'].append({'provider':provider,'base_url':base,'thinking':thinking,'effort':effort,'messages':messages,'tools':tools,'expected':asyncio.run(llm_payload(provider,base,thinking,effort,messages,tools))})
target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(f'Exported current Python policy contracts from {EXPECTED[:7]}.')
