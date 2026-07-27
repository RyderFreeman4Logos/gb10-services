import sys, os, uuid, json, time

sys.path.insert(0, "/home/obj/tmp/Aeon-Bench-Pod-issue14/mvp")
os.chdir("/home/obj/tmp/Aeon-Bench-Pod-issue14/mvp")

from aeon import runner, db, suite as suite_mod
from aeon.targets import TargetError

run_id = "qf18014-" + uuid.uuid4().hex[:10]
params = {"temperature": 0.6, "max_tokens": 8192}
model = "aeon-ultimate"
target_url = "http://100.105.4.92:18014/v1"

target = runner.build_target(model, target_url)

env = {"runner": "aeon-mvp-resilient"}
db.create_run(
    run_id, model=model, target_url=target_url,
    judge_model=None, judge_is_self=False,
    suite_id=suite_mod.SUITE_ID, suite_hash=suite_mod.suite_hash(),
    n_cases=len(suite_mod.CASES), params=params, env=env,
)

base_tok = params.get("max_tokens", 8192)
total = len(suite_mod.CASES)
passed = 0

print(f"Starting RESILIENT benchmark run_id={run_id} cases={total}", flush=True)

for i, case in enumerate(suite_mod.CASES):
    cid = case["id"]
    user = {"role": "user", "content": case["prompt"], "_case_id": cid}

    text = ""
    speed = {}
    status = "scored"

    try:
        resp = target.chat([user], temperature=params["temperature"], max_tokens=base_tok)
        text = resp.get("text", "")
        speed = {k: resp.get(k) for k in ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens", "streamed")}
        status = "scored"
    except TargetError as e:
        text = ""
        speed = {}
        status = "loop_retry_exhausted"
        print(f"  case {i+1}/{total} {cid}: TARGET_ERROR (scored as 0)", flush=True)
    except Exception as e:
        text = ""
        speed = {}
        status = f"gen_error: {repr(e)[:120]}"
        print(f"  case {i+1}/{total} {cid}: GEN_ERROR {repr(e)[:80]}", flush=True)

    try:
        from aeon.evaluators import evaluate
        score, evidence = evaluate(case, text, None)
    except Exception as e:
        score, evidence = 0.0, {"error": f"eval error: {repr(e)[:120]}"}

    db.save_result(
        run_id, cid,
        category=case.get("category", ""),
        tier=case.get("tier", 0),
        status=status,
        score=score,
        raw_output=text,
        evidence=evidence,
        speed=speed,
    )

    if score is not None and score >= 1.0:
        passed += 1

    if (i + 1) % 10 == 0:
        print(f"  progress: {i+1}/{total} passed={passed}", flush=True)

print(f"Benchmark complete run_id={run_id} cases={total} passed={passed}", flush=True)
