import asyncio
from unittest.mock import AsyncMock, patch
from latency_lab import directory_email as mail, provider_directory as directory
from latency_lab.action_tools import ActionToolCall
from latency_lab.tests.test_directory_locations import PipelineChecks
from latency_lab.tests.test_directory_email import EmailTests, HTTP

class NaturalEmailFlow(PipelineChecks):
    async def setup_email(self):
        self.call=ActionToolCall('requested-mail',mail.TOOL_NAME,{})
        self.p.directory_email.prepare=lambda args:{'status':'ready','message':"I'll send that to the email we have for you.",'data':{}}
        self.p.directory_email.send=AsyncMock(return_value={'status':'success','message':"That's on its way.",'data':{'provider_status':'accepted'}})

    async def test_request_sends_without_another_confirmation(self):
        await self.setup_email()
        await self.p._prepare_directory_email(self.attempt,self.call)
        self.p.directory_email.send.assert_awaited_once()
        self.assertIsNone(self.p.pending_action)
        phases=[x.kwargs['phase'] for x in self.p._speak_text.await_args_list]
        self.assertEqual(phases,['directory_email_sending','directory_email_feedback'])
        self.assertIn('success',self.attempt.tool_messages[-1]['content'])

    async def test_uncommitted_request_cannot_send(self):
        await self.setup_email();self.attempt.authorized=False
        task=asyncio.create_task(self.p._prepare_directory_email(self.attempt,self.call))
        await asyncio.sleep(0)
        self.p.directory_email.send.assert_not_awaited()
        self.attempt.cancelled=True;self.attempt.authorization_event.set()
        await task
        self.p.directory_email.send.assert_not_awaited()

    async def test_email_reports_progress_while_waiting(self):
        await self.setup_email();done=asyncio.Event();real_wait=asyncio.wait
        async def send(http):
            await done.wait()
            return {'status':'success','message':'Accepted for sending.','data':{}}
        self.p.directory_email.send=AsyncMock(side_effect=send)
        async def speak(attempt,text,phase):
            if phase=='directory_email_wait':done.set()
        self.p._speak_text=AsyncMock(side_effect=speak)
        async def short_wait(tasks,timeout):return await real_wait(tasks,timeout=0.001)
        with patch('latency_lab.pipeline.asyncio.wait',short_wait):
            await self.p._prepare_directory_email(self.attempt,self.call)
        self.assertIn('directory_email_wait',[x.kwargs['phase'] for x in self.p._speak_text.await_args_list])

    async def test_benefits_do_not_search_or_ask_location(self):
        with patch.object(directory,'lookup',AsyncMock()) as read:
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('benefits',directory.TOOL_NAME,{'intent':'benefits','plan':'Promax'}))
        read.assert_not_awaited()
        text=self.p._speak_text.call_args.args[1]
        self.assertIn("benefit schedule",text)
        self.assertNotIn('area',text)
        self.p._stream_llm_and_tts.assert_not_awaited()

    async def test_acknowledgements_vary_and_never_say_of_course(self):
        with patch.object(directory,'lookup',AsyncMock(return_value={'status':'ok','data':{}})):
            for _ in range(3):
                await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('lookup',directory.TOOL_NAME,{'intent':'lookup','plan':'Plus','town':'Ikeja'}))
        texts=[x.args[1] for x in self.p._speak_text.await_args_list if x.kwargs['phase']=='provider_lookup_acknowledgement']
        self.assertEqual(len(set(texts)),3)
        self.assertTrue(all('of course' not in x.lower() for x in texts))

class RefreshFailure(EmailTests):
    async def test_refresh_failure_is_not_reported_as_missing_hospitals(self):
        self.mail.prepare({});http=HTTP()
        with patch.object(directory,'lookup',AsyncMock(return_value={'status':'failed','data':{'error_type':'TimeoutError'}})):
            result=await self.mail.send(http)
        self.assertEqual(result['status'],'failed')
        self.assertIn("couldn't refresh",result['message'])
        self.assertNotIn("couldn't confirm matching hospitals",result['message'])
        self.assertEqual(result['data']['source_error'],'TimeoutError')
        self.assertEqual(http.calls,[])
