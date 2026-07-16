"""
AEON DFlash CUDA Hang Guard — monkey-patch for vLLM V1

Prevents CUDA graph replay hangs by periodically forcing eager execution
(every HANG_GUARD_FLUSH_INTERVAL steps), breaking any accumulating CUDA
graph state corruption on Blackwell sm_121.

Process failures converge through the vLLM systemd service's restart policy.

Env vars:
  HANG_GUARD_FLUSH_INTERVAL  - steps between forced eager execution (default: 5000)
  HANG_GUARD_DISABLE         - set to "1" to disable all patches
"""

import functools
import logging
import os

logger = logging.getLogger("vllm.hang_guard")

FLUSH_INTERVAL = int(os.environ.get("HANG_GUARD_FLUSH_INTERVAL", "5000"))

_applied = False


def apply():
    global _applied
    if _applied:
        return
    if os.environ.get("HANG_GUARD_DISABLE") == "1":
        logger.info("hang_guard: disabled via HANG_GUARD_DISABLE=1")
        return
    _applied = True

    _patch_determine_batch()
    logger.info(
        "hang_guard: patches applied (flush_interval=%d)",
        FLUSH_INTERVAL,
    )


def _patch_determine_batch():
    """Periodically force eager execution to prevent CUDA graph state corruption."""
    from vllm.v1.worker.gpu_model_runner import GPUModelRunner

    original = GPUModelRunner._determine_batch_execution_and_padding

    @functools.wraps(original)
    def patched(self, *args, force_eager=False, **kwargs):
        if not hasattr(self, "_hg_step"):
            self._hg_step = 0
        self._hg_step += 1

        if self._hg_step % FLUSH_INTERVAL == 0:
            force_eager = True
            logger.info("hang_guard: periodic eager flush at step %d", self._hg_step)

        return original(self, *args, force_eager=force_eager, **kwargs)

    GPUModelRunner._determine_batch_execution_and_padding = patched
