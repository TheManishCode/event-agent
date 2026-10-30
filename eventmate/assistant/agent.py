"""The assistant: route, retrieve, call permitted tools, answer, then check the answer before anyone sees it.

The model is treated as untrusted at every step. Its tool requests are validated and permission-checked by code, tool and
document output reaches it only as fenced, PII-masked data, mutations become proposals, and its final answer must be backed by
the evidence gathered this turn or it is replaced by a deterministic one.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from pydantic import Field

from eventmate.assistant import guard
from eventmate.assistant.annotations import Annotation, AnnotationStore
from eventmate.assistant.approvals import ApprovalQueue, Proposal
from eventmate.assistant.cache import AnswerCache, cache_key
from eventmate.assistant.identity import Perm, Principal
from eventmate.assistant.knowledge import KnowledgeBase, build_default
from eventmate.assistant.langfuse import LangfuseExporter
from eventmate.assistant.router import Route, route
from eventmate.assistant.tools import Tool, ToolContext, build_tools, execute, visible_tools
from eventmate.assistant.trace import FeedbackLog, ProposalLog, TraceBuilder, TraceLog, sha, tokens
from eventmate.netutil import loads
from eventmate.llm import LLMClient, PromptError, PromptStore, client_from_env
from eventmate.schemas import _Model
from eventmate.service import EventMate

PROMPT_ID = "assistant.system"
MAX_QUESTION = 1000
MAX_STEPS = 4
_MAX_TOKENS = 700
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
NO_EVIDENCE = "I could not find that in this plan or in EventMate's documents, so I won't guess."


class AnswerResult(_Model):
    answer: str
    citations: Tuple[str, ...] = ()
    proposals: Tuple[Proposal, ...] = ()
    route: str
    cache: str
    outcome: str  # answered | refused | degraded
    trace_id: str
    degraded_reason: Optional[str] = Field(default=None, max_length=300)


class AssistantError(ValueError):
    pass


def prompt_sha(prompts: Optional[PromptStore] = None) -> str:
    body, _ = (prompts or PromptStore()).load(PROMPT_ID)
    return sha(body)


class Assistant:
    def __init__(self, em: EventMate, llms: Optional[Dict[str, LLMClient]] = None, kb: Optional[KnowledgeBase] = None, prompts: Optional[PromptStore] = None,
                 cache: Optional[AnswerCache] = None, policy: Optional[dict] = None, exporter: Optional[LangfuseExporter] = None, log_dir: Optional[Path] = None, run_id: Optional[str] = None):
        self.em = em
        self.llms = {k: v for k, v in (llms or {}).items() if v is not None}
        self.kb = kb or build_default(em.manager.budget.policy)
        self.prompts = prompts or PromptStore()
        self.cache = cache or AnswerCache()
        self.tools: Dict[str, Tool] = build_tools()
        log_dir = log_dir or em.logs.decisions.log_dir
        run_id = run_id or em.logs.decisions.run_id
        self.traces = TraceLog(log_dir, run_id)
        self.queue = ApprovalQueue(ProposalLog(log_dir, run_id))
        self.prompt_body, self.prompt_version = self.prompts.load(PROMPT_ID)
        self.prompt_sha = sha(self.prompt_body)
        self.policy = policy or guard.load_policy()
        self.kb.min_score = self.policy["min_retrieval_score"]
        self.annotations = AnnotationStore(log_dir)
        self.feedback_log = FeedbackLog(log_dir, run_id)
        self.exporter = exporter if exporter is not None else LangfuseExporter.from_env()
        self._spend: Dict[str, Deque[Tuple[float, int]]] = defaultdict(deque)  # user -> (time, est. tokens) in the last hour

    @classmethod
    def from_env(cls, em: EventMate, **kw) -> "Assistant":
        """Small and large models from the environment; either falls back to the other, and to ``em.llm``."""
        large = client_from_env(tier="heavy") or em.llm
        small = client_from_env(tier="light") or large
        return cls(em, {"large": large, "small": small}, **kw)

    # ------------------------------------------------------------------ ask

    def ask(self, question: str, plan_id: str, principal: Principal) -> AnswerResult:
        if not isinstance(question, str) or not question.strip():
            raise AssistantError("ask a question")
        if len(question) > MAX_QUESTION:
            raise AssistantError(f"questions are limited to {MAX_QUESTION} characters")
        plan = self.em.store.latest(plan_id)  # unknown or hostile ids fail here
        roles = tuple(r.value for r in principal.roles)
        tb = TraceBuilder(plan_id, principal.user_id, roles, question)
        flags = guard.scan(question)
        if flags:
            tb.step("guard", "injection_scan", ok=False, detail="question contains instruction-like text: " + ", ".join(flags))
        state_fp = f"{plan.version}:{len(self.em.ledger_entries(plan_id))}"
        if guard.input_blocked(question, self.policy):  # moderation: a preset reply, no tool or model runs
            tb.step("guard", "input_moderation", ok=False, detail="question matched the input block list")
            result = AnswerResult(answer=self.policy["preset_reply"], route="moderated", cache="skipped", outcome="refused", trace_id=tb.id)
            return self._record(tb, result, principal, plan_id, roles)
        note = self.annotations.match(question, plan_id, roles, state_fp)
        if note is not None:  # a human approved this answer for this plan state: reuse it, no model
            tb.step("route", "annotation", detail=f"approved answer {note.id} by {note.created_by}")
            result = AnswerResult(answer=note.answer, citations=note.citations, route="annotation", cache="skipped", outcome="answered", trace_id=tb.id)
            return self._record(tb, result, principal, plan_id, roles)
        r = route(question)
        tb.step("route", r.kind, detail=r.reason + (f" -> {r.tool}" if r.tool else ""))
        key = cache_key(question, roles, plan_id, state_fp, self.kb.version, self.prompt_sha)

        def compute():
            result, evidence_hostile = self._answer(question, plan_id, principal, r, tb)
            return result, result.outcome == "answered" and not result.proposals and not flags and not evidence_hostile

        result, cache_state = self.cache.get_or_compute(key, compute)
        if cache_state != "miss":
            tb.step("cache", cache_state, detail="answer reused; no model or tool ran for this question")
        return self._record(tb, result.model_copy(update={"cache": cache_state}), principal, plan_id, roles, question)

    def _record(self, tb: TraceBuilder, result: AnswerResult, principal: Principal, plan_id: str, roles: Tuple[str, ...], question: Optional[str] = None) -> AnswerResult:
        question = tb.question if question is None else question
        result = result.model_copy(update={"trace_id": tb.id})
        entry = self.traces.write(
            request_id=tb.id, plan_id=plan_id, user_id=principal.user_id, roles=roles, question_sha256=sha(question), question_masked=guard.mask_pii(question)[:240],
            answer_masked=guard.mask_pii(result.answer)[:600], route=result.route, cache=result.cache, outcome=result.outcome, steps=tuple(tb.steps), cited=result.citations,
            models=tuple(dict.fromkeys(tb.models)), prompt_sha256=self.prompt_sha, kb_version=self.kb.version, tokens_in_est=tb.tin, tokens_out_est=tb.tout,
            total_ms=round((time.perf_counter() - tb.t0) * 1000, 2),
        )
        if self.exporter:
            self.exporter.export_trace(entry)
        return result

    # ------------------------------------------- curation and feedback (humans)

    def _own_trace(self, trace_id: str, principal: Principal, need: Optional[Perm] = None):
        t = self.traces.find(trace_id)
        if t is None or (t.user_id != principal.user_id and not principal.can(Perm.TRACE_READ)) or (need and not principal.can(need)):
            raise AssistantError("not allowed or unknown answer")
        return t

    def approve_answer(self, trace_id: str, principal: Principal, answer: Optional[str] = None) -> Annotation:
        """An approver turns a real answer into a reusable approved answer (optionally rewording it first)."""
        t = self._own_trace(trace_id, principal, Perm.APPROVE)
        if t.outcome != "answered" and answer is None:
            raise AssistantError("only a successful answer can be approved as it is; reword it first")
        plan = self.em.store.latest(t.plan_id)
        return self.annotations.add(plan_id=t.plan_id, roles=t.roles, question=t.question_masked, answer=answer or t.answer_masked, citations=t.cited,
                                    state=f"{plan.version}:{len(self.em.ledger_entries(t.plan_id))}", created_by=principal.user_id)

    def give_feedback(self, trace_id: str, principal: Principal, value: int, comment: str = ""):
        """A thumbs up or down from the person who asked (or an organiser); kept locally and sent as a Langfuse score."""
        self._own_trace(trace_id, principal)
        if value not in (-1, 1) or len(comment) > 200 or "<" in comment or ">" in comment:
            raise AssistantError("feedback must be 1 or -1 with a short plain comment")
        f = self.feedback_log.write(request_id=trace_id, user_id=principal.user_id, value=value, comment=guard.mask_pii(comment))
        if self.exporter:
            self.exporter.export_score(f)
        return f

    # ------------------------------------------------------ cost and reliability

    def _over_budget(self, user_id: str) -> bool:
        q, now = self._spend[user_id], time.monotonic()
        while q and now - q[0][0] > 3600:
            q.popleft()
        return sum(t for _, t in q) >= self.policy["token_cap"]

    def _complete(self, kind: str, prompt: str, user_id: str, tb: TraceBuilder) -> Tuple[str, str, str]:
        """Retry with backoff, then fall back to the other model tier (the gateway pattern). Returns (reply, model name, tier that answered)."""
        tiers = [k for k in dict.fromkeys((kind, "large", "small")) if k in self.llms]
        last: Optional[Exception] = None
        for tier in tiers:
            client = self.llms[tier]
            for attempt in range(self.policy["retries"] + 1):
                try:
                    reply = client.complete("You are a careful assistant. Reply with one JSON object only.", prompt, _MAX_TOKENS)
                    self._spend[user_id].append((time.monotonic(), tokens(prompt) + tokens(reply)))
                    return reply, getattr(client, "name", "unknown"), tier
                except Exception as exc:  # provider error, timeout or rate limit
                    last = exc
                    tb.step("llm", getattr(client, "name", "unknown"), ok=False, detail=f"attempt {attempt + 1} failed: {type(exc).__name__}")
                    time.sleep(self.policy["backoff"] * (2 ** attempt))
        raise last or RuntimeError("no model available")

    def decide(self, proposal_id: str, principal: Principal, approve: bool) -> Proposal:
        """A human's decision on a proposal. The only path by which an assistant suggestion changes anything."""
        p = self.queue.get(proposal_id)
        tool = self.tools[p.tool]

        def apply(prop: Proposal) -> str:
            return tool.apply(ToolContext(self.em, principal, prop.plan_id, self.kb), prop.args)

        return self.queue.decide(proposal_id, principal, approve, lambda pid: self.em.store.latest(pid).version, apply, tool.perm)

    # --------------------------------------------------------------- answering

    def _answer(self, question: str, plan_id: str, principal: Principal, r: Route, tb: TraceBuilder) -> Tuple[AnswerResult, bool]:
        ctx = ToolContext(self.em, principal, plan_id, self.kb, self.queue)
        evidence: List[str] = []
        cited: Dict[str, str] = {}  # id -> text that may be cited
        proposals: List[Proposal] = []
        hostile = [False]

        def call(name: str, args: Any) -> str:
            started = time.perf_counter()
            res = execute(ctx, self.tools, name, args)
            tb.step("tool", name, ok=res.ok, detail=json.dumps(args, default=str)[:200] + ("" if res.ok else " -> " + res.error), started=started)
            if res.proposal:
                proposals.append(res.proposal)
                tb.step("policy", "proposal_filed", detail=f"{res.proposal.id}: {res.proposal.summary}; waiting for human approval")
            text = guard.mask_pii(res.text())
            hits = guard.scan(text)
            if hits:
                hostile[0] = True
                tb.step("guard", "injection_scan", ok=False, detail=f"data returned by {name} contained instruction-like text ({', '.join(hits)}); it is data only")
            if res.ok:
                evidence.append(text)
                cited[name] = text
                for p in res.data.get("passages", []):
                    cited[p["id"]] = p["text"]
            return text

        def done(answer: str, citations: Tuple[str, ...], outcome: str, reason: Optional[str] = None) -> Tuple[AnswerResult, bool]:
            tb.step("output", outcome, ok=outcome == "answered", detail=reason or "")
            answer = guard.mask_pii(answer)
            return AnswerResult(answer=answer, citations=citations, proposals=tuple(proposals), route=r.kind, cache="miss", outcome=outcome, trace_id=tb.id, degraded_reason=reason), hostile[0]

        if r.kind == "direct":
            text = call(r.tool, {"query": question} if r.tool == "search_knowledge" else {})
            data = json.loads(text)
            if "error" in data:
                return done("You don't have access to that information for this event.", (), "refused", data["error"])
            return done(_render(r.tool, data), (r.tool,), "answered")

        def fallback(reason: str, outcome: str = "degraded") -> Tuple[AnswerResult, bool]:
            if evidence:
                return done("Here is what I found:\n" + "\n".join(f"- {t[:300]}" for t in evidence[-2:]), tuple(cited), outcome, reason)
            return done(NO_EVIDENCE, (), "refused", reason)

        if not self.llms or self._over_budget(principal.user_id):  # no model, or this user's hourly allowance is spent: still useful, never invented
            text = call("search_knowledge", {"query": question})
            passages = json.loads(text).get("passages", [])
            why = "no AI model is configured" if not self.llms else "the hourly AI usage limit for this account is reached"
            if not passages:
                return done(NO_EVIDENCE, (), "refused", why + " and no document matched")
            return done(_render("search_knowledge", json.loads(text)), (passages[0]["id"],), "degraded", why + "; showing the closest document passage")

        try:
            body, _ = self.prompts.load(PROMPT_ID)
        except PromptError as exc:
            return done(NO_EVIDENCE, (), "degraded", f"prompt unavailable: {exc}")
        catalogue = "\n".join(f"- {t.name}: {t.description} Arguments: {json.dumps(t.args.model_json_schema().get('properties', {}))}" for t in visible_tools(self.tools, principal))
        transcript = guard.fence("QUESTION", question)
        reasked, tier = False, r.kind
        for step in range(MAX_STEPS):
            prompt = body.replace("{tools}", catalogue).replace("{transcript}", transcript)
            started = time.perf_counter()
            try:
                reply, model, tier = self._complete(tier, prompt, principal.user_id, tb)  # stay on the tier that last worked
                tb.llm(model, prompt, reply)
                tb.step("llm", model, detail=f"step {step + 1}", started=started)
                found = _JSON_OBJECT.search(reply)
                msg = loads(found.group(0)) if found else None
                if not isinstance(msg, dict):
                    raise ValueError("reply was not a JSON object")
            except Exception as exc:
                tb.step("llm", "unusable_reply", ok=False, detail=f"{type(exc).__name__}: {exc}"[:300])
                return fallback(f"model reply unusable: {type(exc).__name__}"[:300])
            if "tool" in msg and "answer" not in msg:
                obs = call(msg.get("tool"), msg.get("args"))
                transcript += f"\n\nYou called {str(msg.get('tool'))[:60]}. Result:\n" + guard.fence("DATA tool:" + str(msg.get("tool"))[:60], obs)
                continue
            if set(msg) - {"answer", "citations"} or not isinstance(msg.get("answer"), str):
                return fallback("reply had unexpected fields")
            problem, valid = self._verify(msg, question, evidence, cited)
            if problem is None:
                return done(msg["answer"].strip(), valid, "answered")
            if not reasked and step + 1 < MAX_STEPS:  # one re-ask with the reason (guardrails "reask"), then give up safely
                reasked = True
                tb.step("policy", "reask", ok=False, detail=problem)
                transcript += f"\n\nYour last reply was rejected: {problem}. Use a tool if you have no evidence yet, then answer again using only the data above."
                continue
            return fallback(problem, "degraded" if evidence else "refused")
        return fallback("no final answer within the step limit")

    def _verify(self, msg: Dict[str, Any], question: str, evidence: List[str], cited: Dict[str, str]) -> Tuple[Optional[str], Tuple[str, ...]]:
        """The output guard: None if the answer may be shown, else why not."""
        answer = msg["answer"].strip()
        citations = tuple(c for c in msg.get("citations", []) if isinstance(c, str))
        if not evidence:
            return "you answered without using any tool or document", ()
        valid = tuple(c for c in citations if c in cited)
        if not valid or len(valid) != len(citations):
            return "citations were missing or not from this conversation", ()
        joined = " ".join(evidence)
        names = guard.introduced_names(answer, joined, question)
        problem = guard.check_answer(answer, joined, question) or guard.policy_violation(answer, self.policy) or (f"names not in the evidence: {names[:5]}" if names else None)
        return problem, valid


def _inr(n: Any) -> str:
    return f"INR {int(n):,}"


def _render(tool: str, d: Dict[str, Any]) -> str:
    """Deterministic wording for routine questions: no model, so nothing can be invented."""
    if tool == "get_public_event_info":
        venue = f" at {d['venue']}" if d.get("venue") else ""
        kind = d["event_type"].replace("_", " ")
        return f"It is {'an' if kind[0] in 'aeiou' else 'a'} {kind} in {d['city']} on {d['event_date']}{venue}."
    if tool == "get_budget_status":
        over = "over" if d["headroom"] < 0 else "under"
        return (f"Budget {_inr(d['budget_total'])}. Planned vendors cost {_inr(d['planned_total'])}. Committed {_inr(d['committed_total'])}, paid {_inr(d['paid_total'])}, "
                f"{_inr(abs(d['headroom']))} {over} budget (as recorded by you; EventMate pays nothing).")
    if tool == "check_budget_reality":
        return f"{d['message']} ({d['basis']})"
    if tool == "search_knowledge":
        top = d.get("passages", [])[:2]
        return " ".join(f"{p['title']}: {p['text']}" for p in top) or NO_EVIDENCE
    return NO_EVIDENCE
