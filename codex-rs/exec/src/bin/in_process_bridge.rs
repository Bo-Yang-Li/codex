use std::collections::HashSet;
use std::future::pending;
use std::sync::Arc;

use clap::Parser;
use codex_app_server_client::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
use codex_app_server_client::InProcessAppServerClient;
use codex_app_server_client::InProcessClientStartArgs;
use codex_app_server_client::InProcessServerEvent;
use codex_app_server_protocol::ClientNotification;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::ConfigWarningNotification;
use codex_app_server_protocol::InitializeParams;
use codex_app_server_protocol::InitializeResponse;
use codex_app_server_protocol::JSONRPCError;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::JSONRPCMessage;
use codex_app_server_protocol::JSONRPCNotification;
use codex_app_server_protocol::JSONRPCRequest;
use codex_app_server_protocol::JSONRPCResponse;
use codex_app_server_protocol::RequestId;
use codex_arg0::Arg0DispatchPaths;
use codex_arg0::arg0_dispatch_or_else;
use codex_core::config::Config;
use codex_core::config_loader::CloudRequirementsLoader;
use codex_core::config_loader::LoaderOverrides;
use codex_core::default_client::get_codex_user_agent;
use codex_feedback::CodexFeedback;
use codex_protocol::protocol::SessionSource;
use codex_utils_cli::CliConfigOverrides;
use serde_json::Value;
use tokio::io::AsyncBufReadExt;
use tokio::io::AsyncWriteExt;
use tokio::io::BufReader;
use tokio::io::BufWriter;
use tracing::warn;

#[derive(Debug, Parser)]
struct BridgeArgs {
    #[clap(flatten)]
    config_overrides: CliConfigOverrides,
}

struct BridgeState {
    arg0_paths: Arg0DispatchPaths,
    cli_overrides: Vec<(String, toml::Value)>,
    config: Arc<Config>,
    config_warnings: Vec<ConfigWarningNotification>,
    client: Option<InProcessAppServerClient>,
    pending_server_requests: HashSet<RequestId>,
}

impl BridgeState {
    fn new(
        arg0_paths: Arg0DispatchPaths,
        cli_overrides: Vec<(String, toml::Value)>,
        config: Arc<Config>,
        config_warnings: Vec<ConfigWarningNotification>,
    ) -> Self {
        Self {
            arg0_paths,
            cli_overrides,
            config,
            config_warnings,
            client: None,
            pending_server_requests: HashSet::new(),
        }
    }

    async fn ensure_client(&mut self, params: &InitializeParams) -> Result<(), JSONRPCErrorError> {
        if self.client.is_some() {
            return Ok(());
        }
        let capabilities = params.capabilities.clone().unwrap_or_default();
        let start_args = InProcessClientStartArgs {
            arg0_paths: self.arg0_paths.clone(),
            config: Arc::clone(&self.config),
            cli_overrides: self.cli_overrides.clone(),
            loader_overrides: LoaderOverrides::default(),
            cloud_requirements: CloudRequirementsLoader::default(),
            feedback: CodexFeedback::new(),
            config_warnings: self.config_warnings.clone(),
            session_source: SessionSource::Cli,
            enable_codex_api_key_env: true,
            client_name: params.client_info.name.clone(),
            client_version: params.client_info.version.clone(),
            experimental_api: capabilities.experimental_api,
            opt_out_notification_methods: capabilities
                .opt_out_notification_methods
                .unwrap_or_default(),
            channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
        };
        let client = InProcessAppServerClient::start(start_args)
            .await
            .map_err(|err| internal_error(format!("failed to start in-process bridge: {err}")))?;
        self.client = Some(client);
        Ok(())
    }

    fn client(&self) -> Result<&InProcessAppServerClient, JSONRPCErrorError> {
        self.client
            .as_ref()
            .ok_or_else(|| invalid_request("bridge has not been initialized"))
    }
}

fn main() -> anyhow::Result<()> {
    arg0_dispatch_or_else(|arg0_paths: Arg0DispatchPaths| async move {
        let args = BridgeArgs::parse();
        let cli_overrides = args
            .config_overrides
            .parse_overrides()
            .map_err(|err| anyhow::anyhow!("failed to parse --config overrides: {err}"))?;
        let config = Arc::new(
            Config::load_default_with_cli_overrides(cli_overrides.clone())
                .map_err(|err| anyhow::anyhow!("failed to load config: {err}"))?,
        );
        let config_warnings: Vec<ConfigWarningNotification> = config
            .startup_warnings
            .iter()
            .map(|warning| ConfigWarningNotification {
                summary: warning.clone(),
                details: None,
                path: None,
                range: None,
            })
            .collect();
        let mut state = BridgeState::new(arg0_paths, cli_overrides, config, config_warnings);
        run_bridge(&mut state).await
    })
}

async fn run_bridge(state: &mut BridgeState) -> anyhow::Result<()> {
    let stdin = tokio::io::stdin();
    let stdout = tokio::io::stdout();
    let mut lines = BufReader::new(stdin).lines();
    let mut writer = BufWriter::new(stdout);

    loop {
        tokio::select! {
            line = lines.next_line() => {
                let Some(line) = line? else {
                    break;
                };
                if line.trim().is_empty() {
                    continue;
                }
                handle_incoming_line(state, &line, &mut writer).await?;
            }
            event = next_bridge_event(state), if state.client.is_some() => {
                let Some(event) = event else {
                    break;
                };
                handle_server_event(state, event, &mut writer).await?;
            }
        }
    }

    if let Some(client) = state.client.take() {
        client.shutdown().await?;
    }
    writer.flush().await?;
    Ok(())
}

async fn next_bridge_event(state: &mut BridgeState) -> Option<InProcessServerEvent> {
    match state.client.as_mut() {
        Some(client) => client.next_event().await,
        None => pending::<Option<InProcessServerEvent>>().await,
    }
}

async fn handle_incoming_line(
    state: &mut BridgeState,
    line: &str,
    writer: &mut BufWriter<tokio::io::Stdout>,
) -> anyhow::Result<()> {
    let value: Value = serde_json::from_str(line)?;
    let message: JSONRPCMessage = serde_json::from_value(value.clone())?;
    match message {
        JSONRPCMessage::Request(request) => {
            handle_client_request(state, request, value, writer).await?;
        }
        JSONRPCMessage::Notification(notification) => {
            handle_client_notification(state, notification, value).await?;
        }
        JSONRPCMessage::Response(response) => {
            handle_server_request_response(state, response).await?;
        }
        JSONRPCMessage::Error(error) => {
            handle_server_request_error(state, error).await?;
        }
    }
    Ok(())
}

async fn handle_client_request(
    state: &mut BridgeState,
    request: JSONRPCRequest,
    value: Value,
    writer: &mut BufWriter<tokio::io::Stdout>,
) -> anyhow::Result<()> {
    if request.method == "initialize" {
        let params: InitializeParams =
            serde_json::from_value(request.params.clone().unwrap_or(Value::Null))?;
        state
            .ensure_client(&params)
            .await
            .map_err(|err| anyhow::anyhow!(err.message))?;
        write_message(
            writer,
            &JSONRPCResponse {
                id: request.id,
                result: serde_json::to_value(InitializeResponse {
                    user_agent: get_codex_user_agent(),
                })?,
            },
        )
        .await?;
        return Ok(());
    }

    let client_request: ClientRequest = serde_json::from_value(value)?;
    let result = match state.client() {
        Ok(client) => client.request(client_request).await,
        Err(err) => {
            write_message(
                writer,
                &JSONRPCError {
                    error: err,
                    id: request.id,
                },
            )
            .await?;
            return Ok(());
        }
    };
    match result {
        Ok(Ok(result)) => {
            write_message(
                writer,
                &JSONRPCResponse {
                    id: request.id,
                    result,
                },
            )
            .await?;
        }
        Ok(Err(error)) => {
            write_message(
                writer,
                &JSONRPCError {
                    error,
                    id: request.id,
                },
            )
            .await?;
        }
        Err(err) => {
            write_message(
                writer,
                &JSONRPCError {
                    error: internal_error(format!("request transport failure: {err}")),
                    id: request.id,
                },
            )
            .await?;
        }
    }
    Ok(())
}

async fn handle_client_notification(
    state: &mut BridgeState,
    notification: JSONRPCNotification,
    value: Value,
) -> anyhow::Result<()> {
    if notification.method == "initialized" {
        return Ok(());
    }
    let client = state.client().map_err(|err| anyhow::anyhow!(err.message))?;
    let client_notification: ClientNotification = serde_json::from_value(value)?;
    client.notify(client_notification).await?;
    Ok(())
}

async fn handle_server_request_response(
    state: &mut BridgeState,
    response: JSONRPCResponse,
) -> anyhow::Result<()> {
    if !state.pending_server_requests.remove(&response.id) {
        return Ok(());
    }
    state
        .client()
        .map_err(|err| anyhow::anyhow!(err.message))?
        .resolve_server_request(response.id, response.result)
        .await
        .map_err(anyhow::Error::from)
}

async fn handle_server_request_error(
    state: &mut BridgeState,
    error: JSONRPCError,
) -> anyhow::Result<()> {
    if !state.pending_server_requests.remove(&error.id) {
        return Ok(());
    }
    state
        .client()
        .map_err(|err| anyhow::anyhow!(err.message))?
        .reject_server_request(error.id, error.error)
        .await
        .map_err(anyhow::Error::from)
}

async fn handle_server_event(
    state: &mut BridgeState,
    event: InProcessServerEvent,
    writer: &mut BufWriter<tokio::io::Stdout>,
) -> anyhow::Result<()> {
    match event {
        InProcessServerEvent::ServerRequest(request) => {
            state.pending_server_requests.insert(request.id().clone());
            write_message(writer, &request).await?;
        }
        InProcessServerEvent::ServerNotification(notification) => {
            write_message(writer, &notification).await?;
        }
        InProcessServerEvent::LegacyNotification(notification) => {
            write_message(writer, &notification).await?;
        }
        InProcessServerEvent::Lagged { skipped } => {
            warn!("in-process bridge lagged; skipped {skipped} event(s)");
        }
    }
    Ok(())
}

async fn write_message<T: serde::Serialize>(
    writer: &mut BufWriter<tokio::io::Stdout>,
    message: &T,
) -> anyhow::Result<()> {
    let encoded = serde_json::to_string(message)?;
    writer.write_all(encoded.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(())
}

fn invalid_request(message: impl Into<String>) -> JSONRPCErrorError {
    JSONRPCErrorError {
        code: -32600,
        data: None,
        message: message.into(),
    }
}

fn internal_error(message: impl Into<String>) -> JSONRPCErrorError {
    JSONRPCErrorError {
        code: -32603,
        data: None,
        message: message.into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_app_server_protocol::InitializeCapabilities;
    use pretty_assertions::assert_eq;

    #[test]
    fn invalid_request_uses_json_rpc_invalid_request_code() {
        let err = invalid_request("no initialize");
        assert_eq!(err.code, -32600);
        assert_eq!(err.message, "no initialize");
    }

    #[test]
    fn internal_error_uses_json_rpc_internal_error_code() {
        let err = internal_error("boom");
        assert_eq!(err.code, -32603);
        assert_eq!(err.message, "boom");
    }

    #[test]
    fn initialize_capabilities_default_matches_bridge_expectation() {
        let capabilities = InitializeCapabilities::default();
        assert_eq!(capabilities.experimental_api, false);
        assert_eq!(capabilities.opt_out_notification_methods, None);
    }
}
