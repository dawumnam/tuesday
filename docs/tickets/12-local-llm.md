---
status: open
blocked-by: [02, 11]
---
# 12 Local LLM for examinee personas + AI tool (last step)

Spec: 모델 라우팅; ADR 0003; User stories 53, 54; Further Notes.

**Deferred by the user: do not start until they re-confirm the hosting decision.** Until then every role runs on Gemini 3.8 Flash (thinking high).

Open decision (to revisit with the user): host the local model on
- this machine (local rig), or
- the `ml` server's RTX 6000 Ada (shared with production avatar; ADR 0003 constraints: 1 GPU, `--gpu-memory-utilization≈0.35`, MPS ≈30%, measure avatar peak memory first, SSH tunnel via jump).

Scope once decided:
- OpenAI-compatible provider in the gateway; route examinee + ai_tool roles to it when `use_local_llm=true` (default stays false → Gemini).
- Confirm model choice/ID (Qwen3.8-27B 4bit availability, license, Korean persona quality sample).
- Test: switch on → examinee/ai_tool go to the local provider; off → Gemini.
- README section: serving command, tunnel, switch, and (if `ml`) the ADR 0003 deployment checklist. Update ADR 0003 if the hosting decision changes it.
