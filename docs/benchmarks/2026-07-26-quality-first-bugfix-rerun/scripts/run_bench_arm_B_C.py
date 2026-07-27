import sys, os, uuid, json, time

ARM = os.environ.get("BENCH_ARM", "B")

sys.path.insert(0, "/home/obj/tmp/Aeon-Bench-Pod-issue14/mvp")
os.chdir("/home/obj/tmp/Aeon-Bench-Pod-issue14/mvp")

# Monkey-patch OpenAITarget to support extra body fields
from aeon.targets import OpenAITarget, TargetError

_orig_chat_stream = OpenAITarget._chat_stream
_orig_chat_once = OpenAITarget._chat_once

def _make_chat_stream_extra(extra_body):
    def _chat_stream(self, messages, temperature, max_tokens):
        # Temporarily inject extra_body into self
        self._extra_body = extra_body
        try:
            return _orig_chat_stream(self, messages, temperature, max_tokens)
        finally:
            self._extra_body = {}
    return _chat_stream

# Actually simpler: just override _chat_stream to add enable_thinking to payload
def _patched_stream(self, messages, temperature, max_tokens):
    # Save original and call with modified payload
    import json as _json, urllib.request, urllib.error, time as _time
    payload = {
        "model": self.model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    # Add arm-specific extra body
    if ARM == "B":
        payload["enable_thinking"] = False

    url = self.base_url + "/chat/completions"
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")

    t0 = _time.perf_counter()
    ttft = None
    chunks = 0
    parts = []
    usage = None
    finish = None

    try:
        resp = urllib.request.urlopen(req, timeout=self.timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise TargetError(f"HTTP {e.code} from {self.base_url}: {body}")

    with resp as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if payload_str == "[DONE]":
                break
            try:
                obj = _json.loads(payload_str)
            except _json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if choices:
                if choices[0].get("finish_reason"):
                    finish = choices[0]["finish_reason"]
                delta = choices[0].get("delta") or {}
                c = delta.get("content")
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if (c or reasoning) and ttft is None:
                    ttft = _time.perf_counter() - t0
                if c or reasoning:
                    chunks += 1
                if c:
                    parts.append(c)
            if obj.get("usage"):
                usage = obj["usage"]

    t_last = _time.perf_counter()
    text = "".join(parts)
    e2e = t_last - t0
    out_toks = (usage or {}).get("completion_tokens") or max(1, chunks)
    decode_tps = out_toks / max(0.001, e2e - (ttft or 0)) if ttft else None

    return {
        "text": text,
        "ttft_ms": (ttft * 1000) if ttft else None,
        "decode_tps": decode_tps,
        "e2e_ms": e2e * 1000,
        "output_tokens": out_toks,
        "streamed": True,
        "finish_reason": finish,
    }

OpenAITarget._chat_stream = _patched_stream

from aeon import runner, db, suite as suite_mod

ARM_CONFIG = {
    "B": {"url": "http://100.105.4.92:18010/v1", "model": "aeon-ultimate"},
    "C": {"url": "http://100.105.4.92:18015/v1", "model": "aeon-ultimate"},
}
cfg = ARM_CONFIG[ARM]

run_id = f"qf18014-{uuid.uuid4().hex[:10]}"
params = {"temperature": 0.6, "max_tokens": 8192}
model = cfg["model"]
target_url = cfg["url"]
target = runner.build_target(model, target_url)

env = {"runner": "aeon-mvp-resilient", "arm": ARM}
db.create_run(
    run_id, model=model, target_url=target_url,
    judge_model=None, judge_is_self=False,
    suite_id=suite_mod.SUITE_ID, suite_hash=suite_mod.suite_hash(),
    n_cases=len(suite_mod.CASES), params=params, env=env,
)

base_tok = params.get("max_tokens", 8192)
total = len(suite_mod.CASES)
passed = 0
print(f"Starting RESILIENT benchmark run_id={run_id} arm={ARM} cases={total}", flush=True)

for i, case in enumerate(suite_mod.CASES):
    cid = case["id"]
    user = {"role": "user", "content": case["prompt"], "_case_id": cid}
    text = ""; speed = {}; status = "scored"
    try:
        resp = target.chat([user], temperature=params["temperature"], max_tokens=base_tok)
        text = resp.get("text", "")
        speed = {k: resp.get(k) for k in ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens", "streamed")}
    except TargetError as e:
        status = "loop_retry_exhausted"
        print(f"  case {i+1}/{total} {cid}: TARGET_ERROR", flush=True)
    except Exception as e:
        status = f"gen_error: {repr(e)[:120]}"
        print(f"  case {i+1}/{total} {cid}: GEN_ERROR {repr(e)[:80]}", flush=True)
    try:
        from aeon.evaluators import evaluate
        score, evidence = evaluate(case, text, None)
    except Exception as e:
        score, evidence = 0.0, {"error": f"eval error: {repr(e)[:120]}"}
    db.save_result(run_id, cid, category=case.get("category",""), tier=case.get("tier",0), status=status, score=score, raw_output=text, evidence=evidence, speed=speed)
    if score is not None and score >= 1.0: passed += 1
    if (i + 1) % 10 == 0: print(f"  progress: {i+1}/{total} passed={passed}", flush=True)
print(f"Benchmark complete run_id={run_id} arm={ARM} cases={total} passed={passed}", flush=True)
