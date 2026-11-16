"""Paired openings and explicit no-progress termination around the upstream simulator."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from harness.user_simulator import (ReactUserSimulator, VerifyUserSimulator, STOP_SIGNAL,
                                    UserSimulatorError, _VERIFY_PROMPT)
from harness.models import ChatMessage, Role
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

SIMULATOR_VERSION = 'paired-verified-user-v3-complete-review'
CUSTOMER_SIMULATOR_VERSION = 'contextual-react-customer-v1'
COMPACT_VERIFIER_VERSION = 'reference-rubric-compact-json-v1'

CUSTOMER_RULES = '''
CUSTOMER TURN CONTRACT:
You write the next CUSTOMER message to a separate service agent. The transcript below
labels its speakers explicitly; service_agent messages are never your own speech.
Use the original customer instruction as your source of identity, facts and goals.
On the first turn, directly request the next action required by that instruction.
Asking to perform an action differs from asking whether it was already performed.
If the service agent asks for a value already given in your instruction, supply that
value as the customer, unless the instruction explicitly requires withholding it.
For example, answer a question about your identity with your own known identity;
do not ask the service agent to tell you its identity or its permissions instead.
Preserve exact quoted values and the instruction's phase order, persona, required
pushback and exchange counts. Request a later phase only after the earlier phase is
confirmed. Stop promptly once all stated exit conditions are met. Never invent a
completed action, new task or extra acceptance condition. Return only the existing
structured customer decision, with the message addressed to the service agent.
'''


class ContextualReactUserSimulator(ReactUserSimulator):
    """Reuse React validation with an explicit, temporary customer transcript."""

    async def _generate_valid_decision(self):
        original = self._messages
        transcript = []
        for message in original[1:]:
            if message.content == 'Begin the conversation as the user described in your instruction.':
                continue
            if message.role in (Role.USER, Role.ASSISTANT):
                transcript.append({'speaker': 'customer' if message.role == Role.ASSISTANT else 'service_agent',
                                   'message': message.content})
        system = original[0].model_copy(update={'content': original[0].content + CUSTOMER_RULES})
        payload={
            'original_customer_instruction': self._instruction,
            'visible_dialogue': transcript, 'next_speaker': 'customer',
            'task': 'Write the next customer turn, or stop when the stated goals are complete.',
        }
        if getattr(self,'task_contract',None):
            payload['task_contract']=self.task_contract.model_dump()
            payload['outstanding_exit_conditions']=getattr(self,'pending_conditions',[])
        self._messages = [system, ChatMessage(role=Role.USER, content=json.dumps(payload, ensure_ascii=False))]
        try:
            return await super()._generate_valid_decision()
        finally:
            # Formatting repairs and prompt annotations are never dialogue turns.
            self._messages = original


class SimulatorFidelityError(UserSimulatorError):
    """A user proxy failed role/goal verification; not an agent task failure."""


class FidelityReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    valid: bool
    goals_complete: bool
    continuation: Literal['complete', 'next_step', 'needs_agent_action', 'scripted_pushback', 'needs_information']
    reason: str


class NativeFidelityReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    valid: bool
    candidate_stop: bool
    continuation: Literal['complete', 'needs_agent_action']
    reason: str


class CompactFidelityVerdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=1, max_length=240)
    valid: bool


class ConversationStalled(UserSimulatorError):
    """The same continuing request produced no new observation or business state."""


class PairedUserSimulator(VerifyUserSimulator):
    def __init__(self, *args, opening_path, identity, adapter, arm, repeated_turns=3,
                 verify_fidelity=False, fidelity_attempts=2, verification_mode='local', **kwargs):
        super().__init__(*args, **kwargs)
        self.opening_path = Path(opening_path)
        self.identity = identity
        self.adapter = adapter
        self.arm = arm
        self.repeated_turns = repeated_turns
        self.opening_metadata = None
        self.verify_fidelity = verify_fidelity
        self.fidelity_attempts = fidelity_attempts
        if verification_mode not in ('local', 'upstream', 'compact','audited'):
            raise ValueError('Unknown user verification mode')
        self.verification_mode = verification_mode

    def reset(self, instruction):
        super().reset(instruction)
        self.opening_pending = True
        self.last_progress = None
        self.repeated = 0
        self.last_review = None
        if self.verify_fidelity:
            reminder = ('\nROLE MAPPING: You are the CUSTOMER. Assistant-role history contains your previous CUSTOMER utterances; '
                'user-role history contains the tested AGENT replies. Produce only the next customer utterance or the stop decision. '
                'Never copy an agent answer as if you were the agent. Stop promptly when the stated goals are satisfied. '
                'When the agent asks for information provided in your instruction, supply that information instead of asking the agent for it.')
            self._messages[0] = self._messages[0].model_copy(update={'content': self._messages[0].content+reminder})

    async def _review_decision(self, candidate):
        if self.verification_mode in ('upstream', 'compact'):
            prompt = _VERIFY_PROMPT.format(instruction=self._instruction,
                conversation=self._format_conversation(), response=candidate, stop=STOP_SIGNAL)
            if self.verification_mode=='compact'and candidate!=STOP_SIGNAL:
                # The upstream full prompt also checks completed goals. Those
                # checks belong to STOP; asking for unfinished work is valid.
                behavior=_VERIFY_PROMPT.split('STEP 4: Check behavioral faithfulness:',1)[1].split('Respond with EXACTLY',1)[0]
                prompt=('Review the next CUSTOMER utterance, which does NOT end the dialogue. '
                    'Check behavioral faithfulness only. The requested business work may still be unfinished.\n\n'
                    'Original customer instruction:\n'+self._instruction+'\n\nVisible conversation:\n'+
                    self._format_conversation()+'\n\nProposed CUSTOMER utterance:\n'+candidate+
                    '\n\nBehavioral checks from the upstream verifier:\n'+behavior+
                    '\nA direct request for the first action and an in-scope clarification are valid. '
                    'Reject unsupported identities or facts, role reversal, new unrelated goals, or jumping '
                    'to an explicitly later phase before its prerequisite. Do not require the requested '
                    'action to be completed before the customer can ask for it.')
            messages = [ChatMessage(role=Role.USER, content=prompt)]
            kwargs = {}
            if self.verification_mode == 'compact':
                messages.insert(0, ChatMessage(role=Role.SYSTEM, content=(
                    'Apply the reference customer-faithfulness rubric internally. Return only the compact JSON verdict, '
                    'with a reason of at most 240 characters; do not print the reasoning steps or PASS/FAIL text. '
                    'A proposed customer request is being made now; it need not have been made in earlier history. '
                    'Providing a known identity now is valid after an identity question. Requests for a later phase '
                    'are valid after the earlier phase is confirmed. A direct opening request is valid even '
                    'without a prior question from the agent. An in-scope clarification about an agent reply '
                    'or a repeated request for a still-unmet original goal is valid; it is not a new goal. '
                    'Judge the CUSTOMER faithfulness, not whether the service agent has succeeded. '
                    'Only a STOP candidate requires every goal to '
                    'have already been confirmed. Enforce exchange counts only when the instruction states them.')))
                kwargs['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': 'customer_fidelity_verdict', 'strict': True,
                    'schema': CompactFidelityVerdict.model_json_schema()}}
            response = await self.llm.chat(messages=messages, temperature=0,
                max_tokens=256 if kwargs else 300, **kwargs)
            if response.finish_reason != 'stop':
                raise SimulatorFidelityError(f'Incomplete customer review: finish_reason={response.finish_reason}')
            text = (response.content or '').strip()
            if self.verification_mode == 'compact':
                try:
                    verdict = CompactFidelityVerdict.model_validate_json(text)
                except ValueError as exc:
                    raise SimulatorFidelityError('Invalid compact customer review') from exc
                valid, reason = verdict.valid, verdict.reason
            elif text.upper() == 'PASS' or text.upper().startswith('PASS:'):
                valid, reason = True, ''
            elif text.upper() == 'FAIL' or text.upper().startswith('FAIL:'):
                valid, reason = False, text.partition(':')[2].strip() or 'Response not faithful to instruction'
            else:
                raise SimulatorFidelityError('Invalid reference customer review')
            latest_agent = next((m.content for m in reversed(self._messages) if m.role == Role.USER
                                 and 'Begin the conversation' not in (m.content or '')), None)
            if latest_agent and len(candidate) > 20 and ' '.join(candidate.casefold().split()) == ' '.join(latest_agent.casefold().split()):
                valid, reason = False, 'Respond as the customer instead of copying the agent reply.'
            return NativeFidelityReview(valid=valid, candidate_stop=candidate == STOP_SIGNAL,
                continuation='complete' if candidate == STOP_SIGNAL else 'needs_agent_action', reason=reason)
        # Reuse the upstream verifier's explicit User/Agent transcript mapping.
        content = json.dumps({'instruction': self._instruction,
            'visible_conversation': self._format_conversation(), 'candidate_user_message': candidate}, ensure_ascii=False)
        response = await self.llm.chat(messages=[
            ChatMessage(role=Role.SYSTEM, content=(
                'Review only the faithfulness of a simulated CUSTOMER, not the business success of the tested agent. '
                'Use only the customer instruction and visible dialogue. Do not invent extra goals, require hidden state, or expose grading answers. '
                'A customer must stop once every stated goal/exit condition is met, including any minimum exchanges or required pushback. '
                'An explicit confirmation can satisfy a customer-visible goal; backend correctness is graded separately. '
                'Set valid=false for echoing an agent answer as the customer, role reversal, inventing demands, refusing to supply known requested information, '
                'premature stopping, or continuing after all goals are complete. Explain the specific unmet rule. '
                'continuation=scripted_pushback only when the instruction requires continued insistence; needs_agent_action when waiting for unfinished business work. '
                'Return the requested compact JSON review.')),
            ChatMessage(role=Role.USER, content=content)], temperature=0, max_tokens=400,
            response_format={'type': 'json_schema', 'json_schema': {'name': 'simulator_fidelity_review',
                'strict': True, 'schema': FidelityReview.model_json_schema()}})
        try:
            review = FidelityReview.model_validate_json(response.content or '')
        except Exception as exc:
            raise SimulatorFidelityError('Invalid simulator fidelity review') from exc
        if review.goals_complete != (candidate == STOP_SIGNAL):
            review.valid = False
            review.reason = ('Stop once the stated customer goals are complete.' if review.goals_complete
                             else 'Continue as the customer until the stated exit conditions are met.')
        latest_agent = next((m.content for m in reversed(self._messages) if m.role == Role.USER
                             and 'Begin the conversation' not in (m.content or '')), None)
        normalize = lambda value: ' '.join(value.casefold().split())
        if latest_agent and len(candidate) > 20 and normalize(candidate) == normalize(latest_agent):
            review.valid = False
            review.reason = 'The candidate copies the agent reply. Respond as the customer, or stop if the customer goals are complete.'
        return review

    async def _verified_decision(self):
        if not self.verify_fidelity:
            return await super()._generate_valid_decision()
        original = self._messages
        feedback = None
        for attempt in range(self.fidelity_attempts):
            try:
                self._messages = original
                if feedback:
                    self._messages = self._fidelity_retry_messages(**feedback)
                candidate = await super()._generate_valid_decision()
            finally:
                self._messages = original
            review = await self._review_decision(candidate)
            self.last_review = review
            self.adapter.emit('simulator_fidelity_review', {'attempt': attempt+1, **review.model_dump()})
            if review.valid:
                return candidate
            feedback = {'candidate': candidate, 'reason': review.reason}
        raise SimulatorFidelityError(f'User proxy failed {self.fidelity_attempts} role/goal checks: {feedback["reason"]}')

    def _fidelity_retry_messages(self, candidate, reason):
        """Keep evaluator feedback separate from actual Agent/customer dialogue."""
        correction = (
            '\n\nEVALUATOR FEEDBACK FOR THIS CUSTOMER TURN (not an Agent utterance):\n'
            + json.dumps({'rejected_customer_candidate': candidate, 'verifier_reason': reason}, ensure_ascii=False)
            + '\nReassess the proposed customer message against the original instruction and visible conversation. '
              'Revise the customer decision; do not repeat the tested Agent reply or invent completed actions. '
              'Return the same structured customer decision required by the output contract.')
        if not self._messages or self._messages[0].role != Role.SYSTEM:
            raise SimulatorFidelityError('Simulator has no system instruction for semantic correction')
        system = self._messages[0].model_copy(update={'content': self._messages[0].content + correction})
        return [system, *self._messages[1:]]

    async def _generate_valid_decision(self):
        if not self.opening_pending:
            return await self._verified_decision()
        fingerprint = hashlib.sha256(json.dumps([self.identity, self._instruction],
                                               sort_keys=True).encode()).hexdigest()
        self.opening_path.parent.mkdir(parents=True, exist_ok=True)
        generated_calls = 0
        if self.opening_path.exists():
            record = json.loads(self.opening_path.read_text())
            reused = True
        else:
            before = self.llm.usage['num_calls']
            message = await self._verified_decision()
            generated_calls = self.llm.usage['num_calls'] - before
            proposal = {'fingerprint': fingerprint, 'message': message,
                        'provider_calls': generated_calls, 'source_arm': self.arm}
            with tempfile.NamedTemporaryFile(mode='w', dir=self.opening_path.parent,
                                             prefix='opening-', suffix='.tmp', delete=False) as stream:
                json.dump(proposal, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            try:
                try:
                    os.link(temporary, self.opening_path)
                    record, reused = proposal, False
                except FileExistsError:
                    record, reused = json.loads(self.opening_path.read_text()), True
            finally:
                temporary.unlink()
        if (record.get('fingerprint') != fingerprint or not isinstance(record.get('message'), str)
                or not isinstance(record.get('provider_calls'), int) or record['provider_calls'] < 1):
            raise RuntimeError('Paired opening identity or record changed')
        # Keep subsequent simulator seeds aligned to the canonical opening's
        # request ordinal. Actual provider usage continues to count actual calls.
        self.llm.seed += record['provider_calls'] - generated_calls
        self.opening_pending = False
        self.opening_metadata = {'fingerprint': fingerprint, 'source_arm': record['source_arm'],
                                 'reused': reused,
                                 'message_sha256': hashlib.sha256(record['message'].encode()).hexdigest()}
        self.adapter.emit('paired_user_opening', self.opening_metadata)
        return record['message']

    async def step(self, agent_message):
        # The subclass verifies candidates before they enter history. Keep the
        # upstream React history protocol; do not run Verify.step a second time.
        message = await ReactUserSimulator.step(self, agent_message)
        if STOP_SIGNAL in message:
            return message
        if self.verify_fidelity and (self.verification_mode in ('upstream', 'compact','audited')
                or self.last_review and self.last_review.continuation == 'scripted_pushback'):
            # The native verifier handles explicit multi-phase/pushback goals.
            # Task/turn/time budgets bound native conversations without a second
            # local judgement terminating valid repeated customer requests.
            self.last_progress, self.repeated = None, 0
            return message
        # Changes to tickets or a distinct actual tool observation establish
        # progress. Repeated reads and presentation-only wording do not.
        observations = sorted({json.dumps([call['name'], call['arguments'], call['result']], sort_keys=True)
                               for call in self.adapter.tool_calls
                               if call.get('backend_executed') and call.get('ok')})
        state = self.adapter.environment.get_state()
        progress = json.dumps([' '.join(message.split()), state.get('tickets', {}), observations], sort_keys=True)
        if progress == self.last_progress:
            self.repeated += 1
        else:
            self.last_progress, self.repeated = progress, 1
        if self.repeated >= self.repeated_turns:
            self.adapter.emit('conversation_stalled', {'repeated_turns': self.repeated,
                                                       'turn': self.adapter.turn})
            raise ConversationStalled(f'{self.repeated} repeated requests without new tool evidence or ticket changes')
        return message


class VerifiedContextualUserSimulator(PairedUserSimulator,ContextualReactUserSimulator):
    """Compose the existing paired native-rubric verifier and contextual generator.

    PairedUserSimulator owns retries, verified openings and faithful termination;
    ContextualReactUserSimulator formats only the generation request. Verification
    sees the restored actual dialogue, including customer/agent speaker labels.
    """

    def __init__(self,*args,task_contract=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.task_contract=task_contract;self.pending_conditions=[]

    def reset(self,instruction):
        super().reset(instruction)
        if self.task_contract:self.task_contract.validate_source(instruction)
        self.pending_conditions=[]

    async def _grade_review(self,judge,**kwargs):
        from harness.semantic_grader import SemanticJudgeError
        try:
            return await judge.grade(**kwargs)
        except SemanticJudgeError as exc:
            self.adapter.emit('simulator_review_error',{'stage':kwargs['task_id'],
                'error':f'{type(exc).__name__}: {exc}'})
            # run_task catches the upstream UserSimulatorError family and saves
            # an explicit unknown verdict, allowing the remaining cases to run.
            raise SimulatorFidelityError('Customer evidence review failed: '+str(exc))from exc

    async def _generate_valid_decision(self):
        if self.verification_mode=='audited'and not self.opening_pending:
            review=await self._review_decision(STOP_SIGNAL)
            self.adapter.emit('simulator_terminal_review',review.model_dump())
            if review.valid:return STOP_SIGNAL
        return await super()._generate_valid_decision()

    async def _review_decision(self,candidate):
        if self.task_contract:
            from .customer_contract import review_contract,visible_turns
            from harness.semantic_grader import SemanticJudgeError
            turns=visible_turns(self._messages)
            if candidate==STOP_SIGNAL and not any(t['role']=='assistant'for t in turns):
                return NativeFidelityReview(valid=False,candidate_stop=True,continuation='complete',reason='No service-agent result yet.')
            try:
                result=await review_contract(self.llm,self.task_contract,self._instruction,turns,
                    self.opening_path.parent/'stage_review_cache',candidate=None if candidate==STOP_SIGNAL else candidate)
            except SemanticJudgeError as exc:
                raise SimulatorFidelityError('Atomic customer review failed: '+str(exc))from exc
            if candidate==STOP_SIGNAL:
                self.pending_conditions=[condition.requirement for condition,verdict in zip(
                    self.task_contract.exit_conditions,result.audit['criteria'])if not verdict['met']]
                self.pending_conditions.extend(item['remaining_requirement']for item in
                    result.audit.get('exchange_requirements',[])if not item['passed'])
            self.adapter.emit('customer_contract_review',{'candidate_stop':candidate==STOP_SIGNAL,
                'passed':result.passed,'pending_conditions':self.pending_conditions,'audit':result.audit})
            return NativeFidelityReview(valid=result.passed,candidate_stop=candidate==STOP_SIGNAL,
                continuation='complete'if candidate==STOP_SIGNAL else'needs_agent_action',
                reason='; '.join(result.failures)[:240]if result.failures else'Atomic instruction conditions satisfied.')
        if self.verification_mode!='audited':return await super()._review_decision(candidate)
        from harness.semantic_grader import SemanticJudge,SemanticJudgeConfig
        turns=[{'role':'assistant'if m.role==Role.USER else'user','content':m.content}
            for m in self._messages[1:]if 'Begin the conversation'not in m.content]
        latest=next((t['content']for t in reversed(turns)if t['role']=='assistant'),None)
        if candidate!=STOP_SIGNAL and latest and len(candidate)>20 and ' '.join(candidate.casefold().split())==' '.join(latest.casefold().split()):
            return NativeFidelityReview(valid=False,candidate_stop=False,continuation='needs_agent_action',
                reason='The proposed customer turn copies the service agent reply.')
        stopping=candidate==STOP_SIGNAL
        if stopping and not latest:
            return NativeFidelityReview(valid=False,candidate_stop=True,continuation='complete',
                reason='No service agent response exists to satisfy the customer goals.')
        context=json.dumps({'customer_instruction':self._instruction,'visible_dialogue':turns,
            'proposed_customer_turn':candidate,'review_target':'service-agent completion evidence'if stopping else'proposed customer utterance'},ensure_ascii=False)
        criterion=(
            'The service agent explicitly confirms every requested business outcome or reports every requested fact needed '
            'to satisfy the customer instruction. A promise of future action is insufficient. All phases must be '
            'complete, including any explicitly required customer pushback or exchange count. Quote the actual '
            'completion statements for every required outcome; an unperformed later phase cannot be treated as complete.'
            if stopping else
            'The proposed customer utterance follows the active goal and persona in the customer instruction. It '
            'supplies known information when asked, requests only an outstanding instructed step, or performs '
            'explicitly required pushback. It introduces no new goal or fabricated result. If all instructed goals '
            'are already satisfied and no phase or pushback remains, an additional request does not satisfy this criterion.')
        judge=SemanticJudge(self.llm,SemanticJudgeConfig(provider='openai',model=getattr(self.llm,'model','local'),
            max_tokens=1200,max_attempts=2,endpoint_fingerprint='customer-evidence-review-v2/'+str(getattr(self.llm,'reason_first_grading',False)),
            cache_dir=self.opening_path.parent/'customer_review_cache'))
        source=turns if stopping else[{'role':'assistant','content':candidate}]
        result=await self._grade_review(judge,task_id='customer-fidelity',task_instruction=context,criteria=[criterion],turns=source)
        self.adapter.emit('simulator_evidence_audit',{'candidate_stop':stopping,'passed':result.passed,'audit':result.audit})
        if stopping and not result.passed:
            # Adjudicate a negative decision against the original instruction,
            # rather than asking the generator to repeat the same rejected STOP.
            # Review the original task independently. The initial verdict and
            # explanation stay in audit logs, outside the second judge's input;
            # otherwise the second call can simply echo the first judgement.
            # Keep exact speaker-labeled evidence for native quote validation.
            evidence='\n\n'.join([
                'VISIBLE DIALOGUE:\n'+'\n\n'.join(
                    ('SERVICE AGENT'if t['role']=='assistant'else'CUSTOMER')+':\n'+t['content']
                    for t in turns)])
            critic=SemanticJudge(self.llm,SemanticJudgeConfig(provider='openai',model=getattr(self.llm,'model','local'),
                max_tokens=1200,max_attempts=2,endpoint_fingerprint='customer-independent-completion-v5/'+str(getattr(self.llm,'reason_first_grading',False)),
                cache_dir=self.opening_path.parent/'customer_negative_review_cache'))
            supported=await self._grade_review(critic,task_id='customer-rejection',task_instruction=json.dumps({
                'customer_instruction':self._instruction,'visible_dialogue':turns,
                'proposed_customer_turn':candidate},ensure_ascii=False),criteria=[
                'Every explicit original customer exit condition has already been satisfied in the visible '
                'dialogue. Required business outcomes must be confirmed or requested facts reported. Required '
                'customer challenges, confirmation exchanges and subsequent service-agent reiterations must '
                'already have occurred. A promise alone does not confirm an action. Optional offers of future '
                'help do not create an additional original goal. Cite actual dialogue when confirming completion; '
                'an instruction or a reviewer claim alone does not establish that it happened.'],
                turns=[{'role':'assistant','content':evidence}])
            self.adapter.emit('simulator_rejection_adjudication',{'completion_confirmed':supported.passed,
                'rejection_supported':not supported.passed,
                'initial_audit':result.audit,'adjudication':supported.audit})
            if supported.passed:
                return NativeFidelityReview(valid=True,candidate_stop=True,continuation='complete',
                    reason='Independent dialogue-evidence review confirms all original exit conditions.')
        return NativeFidelityReview(valid=result.passed,candidate_stop=stopping,
            continuation='complete'if stopping else'needs_agent_action',
            reason='; '.join(result.failures)[:240]if result.failures else'Exact-quote audit supports the customer decision.')
