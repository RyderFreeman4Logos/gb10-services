#![forbid(unsafe_code)]

mod proxy;

use std::{ffi::OsString, future::pending, path::PathBuf, process::ExitCode, time::Duration};

use llm_guard_proxy_core::{
    ConfigManager, ObservabilityStore, RequestId, redact_upstream_base_url,
};
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> ExitCode {
    match run(std::env::args_os()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}

async fn run(args: impl IntoIterator<Item = OsString>) -> Result<(), String> {
    let config_path = parse_config_path(args)?;
    let manager = match config_path {
        Some(path) => ConfigManager::from_explicit_path(path),
        None => ConfigManager::from_default_path(),
    }
    .map_err(|error| error.to_string())?;
    let config = manager
        .handle()
        .snapshot()
        .map_err(|error| error.to_string())?;
    let store = ObservabilityStore::open(manager.handle()).map_err(|error| error.to_string())?;
    let _watcher = manager
        .spawn_polling(Duration::from_secs(1))
        .map_err(|error| error.to_string())?;
    let bind_address = format!("{}:{}", config.server.bind_host, config.server.port);
    let listener = TcpListener::bind(&bind_address)
        .await
        .map_err(|error| format!("failed to bind {bind_address}: {error}"))?;
    let local_addr = listener
        .local_addr()
        .map_err(|error| format!("failed to read listener address: {error}"))?;
    let request_id = RequestId::generate();
    eprintln!(
        "{}",
        proxy::render_health(&config, manager.path(), &request_id)
    );
    eprintln!(
        "{}",
        render_listening(local_addr, &config.upstream.base_url)
    );

    let state = proxy::ProxyState::new(
        manager.handle(),
        manager.path().to_path_buf(),
        store,
        proxy::build_http_client().map_err(|error| error.to_string())?,
    );
    proxy::serve_until_shutdown(listener, state, shutdown_signal())
        .await
        .map_err(|error| format!("server failed: {error}"))
}

async fn shutdown_signal() {
    let ctrl_c = async {
        if let Err(error) = tokio::signal::ctrl_c().await {
            eprintln!("failed to install Ctrl-C handler: {error}");
        }
    };

    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(error) => {
                eprintln!("failed to install SIGTERM handler: {error}");
                pending::<()>().await;
            }
        }
    };

    #[cfg(not(unix))]
    let terminate = pending::<()>();

    tokio::select! {
        () = ctrl_c => {}
        () = terminate => {}
    }
}

fn render_listening(local_addr: impl std::fmt::Display, upstream_base_url: &str) -> String {
    format!(
        "llm-guard-proxy listening={local_addr} upstream_base_url={}",
        redact_upstream_base_url(upstream_base_url)
    )
}

fn parse_config_path(args: impl IntoIterator<Item = OsString>) -> Result<Option<PathBuf>, String> {
    let mut args = args.into_iter();
    let _program = args.next();
    let mut config_path = None;

    while let Some(arg) = args.next() {
        if arg == "--config" {
            let Some(path) = args.next() else {
                return Err(String::from("--config requires a path"));
            };
            config_path = Some(PathBuf::from(path));
        } else if let Some(value) = arg
            .to_str()
            .and_then(|value| value.strip_prefix("--config="))
        {
            if value.is_empty() {
                return Err(String::from("--config requires a path"));
            }
            config_path = Some(PathBuf::from(value));
        } else {
            return Err(format!("unknown argument: {}", arg.to_string_lossy()));
        }
    }

    Ok(config_path)
}

#[cfg(test)]
mod tests {
    use std::{ffi::OsString, path::Path};

    use llm_guard_proxy_core::{AppConfig, HeartbeatMode, RequestId};

    use super::{parse_config_path, proxy::render_health, render_listening};

    #[test]
    fn renders_health_with_config_summary() {
        let mut config = AppConfig::default();
        config.heartbeat.mode = HeartbeatMode::JsonWhitespace;
        config.heartbeat.interval_secs = 7;
        config.observability.enabled = false;

        let request_id =
            RequestId::from_string("req-health").expect("test request id should be valid");

        assert_eq!(
            render_health(&config, Path::new("/tmp/config.toml"), &request_id),
            "llm-guard-proxy request_id=req-health readiness=ready license=Apache-2.0 config_path=/tmp/config.toml heartbeat_mode=json-whitespace heartbeat_interval_secs=7 observability_enabled=false"
        );
    }

    #[test]
    fn parses_explicit_config_argument() {
        let args = [
            OsString::from("llm-guard-proxy"),
            OsString::from("--config"),
            OsString::from("dev.toml"),
        ];
        assert_eq!(
            parse_config_path(args).expect("args should parse"),
            Some("dev.toml".into()),
        );
    }

    #[test]
    fn renders_listening_with_redacted_upstream_base_url() {
        let rendered = render_listening(
            "127.0.0.1:18009",
            "https://user:secret@example.test/v1?x-api-key=sk-test&safe=ok#token=sk-test",
        );

        assert!(
            rendered
                .contains("upstream_base_url=https://redacted:redacted@example.test/v1?redacted")
        );
        assert!(!rendered.contains("user"));
        assert!(!rendered.contains("secret"));
        assert!(!rendered.contains("sk-test"));
        assert!(!rendered.contains("x-api-key"));
        assert!(!rendered.contains("safe=ok"));
        assert!(!rendered.contains("token=sk-test"));
    }
}
