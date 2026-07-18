#!/usr/bin/env python3
"""Attested host inspection and peak readiness for the Querit canary."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import querit_deepinfra_adapter
import querit_vllm_artifact
from reranker_equivalence_wire import (
    DEEPINFRA_MODEL_VERSION,
    ENDPOINT_PATH,
    canonical_payload,
    validate_response,
)


TEXT_UNIT = "vllm-aeon-27b-dflash.service"
BACKEND_UNIT = "vllm-querit-4b-canary-backend.service"
ADAPTER_UNIT = "vllm-querit-4b-canary.service"
EMBEDDING_UNIT = "vllm-embedding.service"
PRODUCTION_RERANKER_UNIT = "querit-4b-reranker.service"
LEGACY_RERANKER_UNIT = "vllm-qwen3-reranker-8b.service"
GUARD_UNIT = "llm-guard-proxy.service"
IMMUTABLE_NEIGHBORS = (
    EMBEDDING_UNIT,
    PRODUCTION_RERANKER_UNIT,
    LEGACY_RERANKER_UNIT,
    GUARD_UNIT,
)
MINIMUM_HEADROOM_GIB = 20
DEFAULT_MODEL = Path("/home/obj/models/querit-4b-vllm")
PUBLIC_URL = (
    "http://100.105.4.92:18014" + ENDPOINT_PATH + "?version=" + DEEPINFRA_MODEL_VERSION
)
BACKEND_SCORE_URL = "http://127.0.0.1:18015/score"
MAX_WARMUP_RESPONSE_BYTES = 32 * 1024 * 1024
PEAK_CONTEXT_TOKENS = 32768
PEAK_BATCH_SIZE = 16
MINIMUM_POST_WARM_GIB = 2
CONTAINER_NAMES = {
    TEXT_UNIT: "vllm-aeon-27b-dflash-n12",
    BACKEND_UNIT: "vllm-querit-4b-canary",
    EMBEDDING_UNIT: "vllm-embedding",
    PRODUCTION_RERANKER_UNIT: "querit-4b-reranker",
    LEGACY_RERANKER_UNIT: "vllm-qwen3-reranker-8b",
}


class LifecycleError(RuntimeError):
    """The requested lifecycle transition failed closed."""


@dataclass(frozen=True)
class ServiceState:
    active: bool
    invocation_id: str
    main_pid: int = 0
    control_group: str = ""
    unit_pids: tuple[int, ...] = ()
    container_id: str = ""
    container_pid: int = 0
    container_cgroup: str = ""
    container_pids: tuple[int, ...] = ()

    def record(self) -> dict[str, object]:
        return {
            "active": self.active,
            "container_cgroup": self.container_cgroup,
            "container_id": self.container_id,
            "container_pid": self.container_pid,
            "container_pids": list(self.container_pids),
            "control_group": self.control_group,
            "invocation_id": self.invocation_id,
            "main_pid": self.main_pid,
            "unit_pids": list(self.unit_pids),
        }

    @classmethod
    def from_record(cls, value: object, label: str) -> ServiceState:
        if not isinstance(value, dict) or set(value) != set(cls(False, "").record()):
            raise LifecycleError(f"{label} service snapshot fields are invalid")
        active = value["active"]
        invocation_id = value["invocation_id"]
        text_fields = ("control_group", "container_id", "container_cgroup")
        integer_fields = ("main_pid", "container_pid")
        sequence_fields = ("unit_pids", "container_pids")
        if not isinstance(active, bool) or not isinstance(invocation_id, str):
            raise LifecycleError(f"{label} service snapshot types are invalid")
        if any(not isinstance(value[key], str) for key in text_fields):
            raise LifecycleError(f"{label} service snapshot text is invalid")
        if any(
            isinstance(value[key], bool)
            or not isinstance(value[key], int)
            or value[key] < 0
            for key in integer_fields
        ):
            raise LifecycleError(f"{label} service snapshot PID is invalid")
        parsed_sequences: list[tuple[int, ...]] = []
        for key in sequence_fields:
            sequence = value[key]
            if (
                not isinstance(sequence, list)
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item <= 0
                    for item in sequence
                )
                or sequence != sorted(set(sequence))
            ):
                raise LifecycleError(f"{label} service snapshot PID set is invalid")
            parsed_sequences.append(tuple(sequence))
        return cls(
            active=active,
            invocation_id=invocation_id,
            main_pid=value["main_pid"],
            control_group=value["control_group"],
            unit_pids=parsed_sequences[0],
            container_id=value["container_id"],
            container_pid=value["container_pid"],
            container_cgroup=value["container_cgroup"],
            container_pids=parsed_sequences[1],
        )


class SystemHost:
    def __init__(self, model_root: Path = DEFAULT_MODEL) -> None:
        self.model_root = model_root

    def verify_artifact(self) -> str:
        return querit_vllm_artifact.manifest_sha256(self.model_root)

    def memory_available_gib(self) -> int:
        return self.memory_available_kib() // (1024 * 1024)

    def memory_available_kib(self) -> int:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) == 3 and parts[2] == "kB":
                        return int(parts[1])
        except (OSError, UnicodeError, ValueError) as exc:
            raise LifecycleError("cannot read MemAvailable") from exc
        raise LifecycleError("MemAvailable is missing from /proc/meminfo")

    def service_state(self, unit: str) -> ServiceState:
        completed = self._systemctl(
            "show",
            unit,
            "--property=ActiveState",
            "--property=InvocationID",
            "--property=MainPID",
            "--property=ControlGroup",
        )
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if set(values) != {"ActiveState", "InvocationID", "MainPID", "ControlGroup"}:
            raise LifecycleError(f"systemd state fields are incomplete for {unit}")
        try:
            main_pid = int(values["MainPID"])
        except ValueError as exc:
            raise LifecycleError(f"systemd MainPID is invalid for {unit}") from exc
        active = values["ActiveState"] == "active"
        control_group = values["ControlGroup"]
        unit_pids = self._cgroup_pids(control_group) if control_group else ()
        container_id, container_pid, container_cgroup, container_pids = (
            self._container_state(unit)
        )
        if active and (
            not values["InvocationID"]
            or main_pid <= 0
            or main_pid not in unit_pids
            or unit in CONTAINER_NAMES
            and (not container_id or container_pid not in container_pids)
        ):
            raise LifecycleError(f"active service identity is incomplete for {unit}")
        if not active and (main_pid != 0 or unit_pids or container_pid != 0):
            raise LifecycleError(f"inactive service retains live processes for {unit}")
        return ServiceState(
            active=active,
            invocation_id=values["InvocationID"],
            main_pid=main_pid,
            control_group=control_group,
            unit_pids=unit_pids,
            container_id=container_id,
            container_pid=container_pid,
            container_cgroup=container_cgroup,
            container_pids=container_pids,
        )

    def unit_file_state(self, unit: str) -> str:
        """Return the exact systemd unit-file state used for mask admission.

        The lifecycle only needs this before it takes ownership of stopping text.
        Keeping the query here makes the policy use the same fixed systemctl binary
        as all other lifecycle operations.
        """

        completed = self._systemctl("show", unit, "--property=UnitFileState")
        key, separator, value = completed.stdout.strip().partition("=")
        if key != "UnitFileState" or not separator or not value:
            raise LifecycleError(f"systemd UnitFileState is invalid for {unit}")
        return value

    @staticmethod
    def _cgroup_pids(control_group: str) -> tuple[int, ...]:
        if not control_group.startswith("/") or ".." in Path(control_group).parts:
            raise LifecycleError("cgroup path is unsafe")
        path = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"
        try:
            raw = path.read_text()
            pids = tuple(sorted({int(line) for line in raw.splitlines()}))
        except (OSError, UnicodeError, ValueError) as exc:
            raise LifecycleError(f"cannot read cgroup processes for {control_group}") from exc
        if any(pid <= 0 for pid in pids):
            raise LifecycleError(f"cgroup contains an invalid PID for {control_group}")
        return pids

    @classmethod
    def _process_cgroup(cls, pid: int) -> tuple[str, tuple[int, ...]]:
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        except (OSError, UnicodeError) as exc:
            raise LifecycleError(f"cannot inspect container PID {pid}") from exc
        unified = [line.removeprefix("0::") for line in lines if line.startswith("0::")]
        if len(unified) != 1:
            raise LifecycleError(f"container PID {pid} lacks one cgroup-v2 identity")
        return unified[0], cls._cgroup_pids(unified[0])

    @classmethod
    def _container_state(cls, unit: str) -> tuple[str, int, str, tuple[int, ...]]:
        name = CONTAINER_NAMES.get(unit)
        if name is None:
            return "", 0, "", ()
        try:
            listed = subprocess.run(
                [
                    "/usr/bin/docker",
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{name}$",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            identities = listed.stdout.splitlines()
            if not identities:
                return "", 0, "", ()
            if len(identities) != 1 or re.fullmatch(r"[0-9a-f]{64}", identities[0]) is None:
                raise LifecycleError(f"container lookup is ambiguous for {unit}")
            completed = subprocess.run(
                [
                    "/usr/bin/docker",
                    "container",
                    "inspect",
                    "--format",
                    "{{.Id}}|{{.State.Pid}}|{{.State.Running}}|{{.State.OOMKilled}}",
                    identities[0],
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError(f"cannot inspect container for {unit}") from exc
        fields = completed.stdout.strip().split("|")
        if len(fields) != 4 or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None:
            raise LifecycleError(f"container identity is malformed for {unit}")
        try:
            pid = int(fields[1])
        except ValueError as exc:
            raise LifecycleError(f"container PID is malformed for {unit}") from exc
        running = fields[2] == "true"
        oom_killed = fields[3] == "true"
        if fields[2] not in {"true", "false"} or fields[3] not in {"true", "false"}:
            raise LifecycleError(f"container state is malformed for {unit}")
        if oom_killed:
            raise LifecycleError(f"container was OOM-killed for {unit}")
        if not running:
            if pid != 0:
                raise LifecycleError(f"stopped container retains a PID for {unit}")
            return fields[0], 0, "", ()
        if pid <= 0:
            raise LifecycleError(f"running container lacks a PID for {unit}")
        cgroup, pids = cls._process_cgroup(pid)
        return fields[0], pid, cgroup, pids

    def start(self, unit: str) -> None:
        if unit not in {TEXT_UNIT, BACKEND_UNIT, ADAPTER_UNIT}:
            raise LifecycleError(f"refusing to start out-of-scope unit: {unit}")
        self._systemctl("start", unit, timeout=1900)

    def stop(self, unit: str) -> None:
        if unit not in {TEXT_UNIT, BACKEND_UNIT, ADAPTER_UNIT}:
            raise LifecycleError(f"refusing to stop out-of-scope unit: {unit}")
        self._systemctl("stop", unit, timeout=120)

    def warm(self) -> None:
        backend_before = self.service_state(BACKEND_UNIT)
        adapter_before = self.service_state(ADAPTER_UNIT)
        if not backend_before.active or not adapter_before.active:
            raise LifecycleError("canary units must be active before readiness")
        memory_events_before = self.cgroup_memory_events(BACKEND_UNIT)

        public_response = self._post(
            PUBLIC_URL, canonical_payload(["wire readiness"], ["wire readiness"])
        )
        validate_response(public_response, 1)

        peak_text = "peak-context-allocation " * 40000
        peak_request = querit_deepinfra_adapter.PublicRequest(
            queries=("peak context",),
            documents=(peak_text,),
            instruction=None,
            service_tier=None,
        )
        peak_payload = json.loads(
            querit_deepinfra_adapter.backend_request_bytes(peak_request)
        )
        peak_payload["truncate_prompt_tokens"] = -1
        peak_response = querit_deepinfra_adapter.parse_backend_response(
            self._post(BACKEND_SCORE_URL, _compact_json(peak_payload)), 1
        )
        if peak_response.input_tokens != PEAK_CONTEXT_TOKENS:
            raise LifecycleError("peak warmup did not allocate the 32,768-token context")

        batch_request = querit_deepinfra_adapter.PublicRequest(
            queries=tuple(f"batch query {index}" for index in range(PEAK_BATCH_SIZE)),
            documents=tuple(
                (f"batch document {index} " * 512) for index in range(PEAK_BATCH_SIZE)
            ),
            instruction=None,
            service_tier=None,
        )
        batch_response = querit_deepinfra_adapter.parse_backend_response(
            self._post(
                BACKEND_SCORE_URL,
                querit_deepinfra_adapter.backend_request_bytes(batch_request),
            ),
            PEAK_BATCH_SIZE,
        )
        if batch_response.input_tokens < 4096:
            raise LifecycleError("batch warmup did not exercise chunked prefill")

        memory_events_after = self.cgroup_memory_events(BACKEND_UNIT)
        if memory_events_before != {"oom": 0, "oom_kill": 0} or (
            memory_events_after != memory_events_before
        ):
            raise LifecycleError("canary backend recorded memory pressure or OOM")
        if self.memory_available_gib() < MINIMUM_POST_WARM_GIB:
            raise LifecycleError("canary warmup left insufficient host memory")
        if self.service_state(BACKEND_UNIT) != backend_before:
            raise LifecycleError("backend unit/container/PIDs changed during warmup")
        if self.service_state(ADAPTER_UNIT) != adapter_before:
            raise LifecycleError("adapter unit/PIDs changed during warmup")

    @staticmethod
    def _post(url: str, body: bytes) -> bytes:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                response_body = response.read(MAX_WARMUP_RESPONSE_BYTES + 1)
                if (
                    response.status != 200
                    or len(response_body) > MAX_WARMUP_RESPONSE_BYTES
                ):
                    raise LifecycleError("canary warmup returned invalid HTTP evidence")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LifecycleError("canary warmup transport failed") from exc
        return response_body

    def cgroup_memory_events(self, unit: str) -> dict[str, int]:
        state = self.service_state(unit)
        if not state.container_cgroup:
            raise LifecycleError(f"{unit} has no container cgroup for memory checks")
        if not state.container_cgroup.startswith("/"):
            raise LifecycleError(f"{unit} has an unsafe container cgroup")
        path = (
            Path("/sys/fs/cgroup")
            / state.container_cgroup.lstrip("/")
            / "memory.events"
        )
        try:
            values = {
                key: int(value)
                for key, value in (
                    line.split() for line in path.read_text().splitlines()
                )
            }
        except (OSError, UnicodeError, ValueError) as exc:
            raise LifecycleError(f"cannot read memory events for {unit}") from exc
        if "oom" not in values or "oom_kill" not in values:
            raise LifecycleError(f"memory events are incomplete for {unit}")
        return {"oom": values["oom"], "oom_kill": values["oom_kill"]}

    @staticmethod
    def _systemctl(
        *arguments: str, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["/usr/bin/systemctl", "--user", *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError("systemctl failed: " + " ".join(arguments)) from exc


def _compact_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
