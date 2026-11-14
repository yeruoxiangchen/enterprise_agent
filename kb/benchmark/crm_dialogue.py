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
        self._messages = [system, ChatMessage(role=Role.USER, content=json.dumps({
            'original_customer_instruction': self._instruction,
            'visible_dialogue': transcript, 'next_speaker': 'customer',
            'task': 'Write the next customer turn, or stop when the stated goals are complete.',
        }, ensure_ascii=False))]
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
    valid: bool
    reason: str = Field(min_length=1, max_length=240)


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
        if verification_mode not in ('local', 'upstream', 'compact'):
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
            messages = [ChatMessage(role=Role.USER, content=prompt)]
            kwargs = {}
            if self.verification_mode == 'compact':
                messages.insert(0, ChatMessage(role=Role.SYSTEM, content=(
                    'Apply the reference customer-faithfulness rubric internally. Return only the compact JSON verdict, '
                    'with a reason of at most 240 characters; do not print the reasoning steps or PASS/FAIL text. '
                    'A proposed customer request is being made now; it need not have been made in earlier history. '
                    'Providing a known identity now is valid after an identity question. Requests for a later phase '
                    'are valid after the earlier phase is confirmed. Only a STOP candidate requires every goal to '
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
        if self.verify_fidelity and (self.verification_mode in ('upstream', 'compact')
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
