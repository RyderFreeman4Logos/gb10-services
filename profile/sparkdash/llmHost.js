/**
 * Host used for LLM HTTP (probe, Showcase, DecodeBench, connectivity test).
 *
 * Local Sparks probe loopback: engines like ds4-server (Entrpi/ds4-on-spark
 * via ~/models/ds4f/start.sh) default to `--host 127.0.0.1`, so probing the
 * LAN IP would miss them. Remote Sparks still use lanIp (they must bind a
 * reachable interface or sit behind a tunnel). Decode and prefill benches
 * additionally fall back to an SSH local-forward onto remote loopback when
 * LAN HTTP is closed.
 *
 * Requires the dashboard process to share the host network namespace when
 * running in Docker (see docker-compose `network_mode: host`).
 *
 * GB10 overlay (bbec3bb): this host's vLLM listeners bind Tailnet
 * 100.105.4.92 only, not 127.0.0.1. When lanIp is set, prefer it so a local
 * Spark can still collect sysfs/nvidia-smi while probing the live LLM ports.
 * Upstream `isLocal → 127.0.0.1` is unchanged when lanIp is empty.
 *
 * @param {{ isLocal?: boolean, lanIp?: string } | null | undefined} spark
 * @returns {string}
 */
export function llmProbeHost(spark) {
  const ip = spark?.lanIp != null ? String(spark.lanIp).trim() : "";
  if (ip) return ip;
  if (spark?.isLocal) return "127.0.0.1";
  return ip;
}
