use crate::auth::AuthProvider;
use crate::common::ResponseStream;
use crate::common::ResponsesApiRequest;
use crate::endpoint::session::EndpointSession;
use crate::error::ApiError;
use crate::provider::Provider;
use crate::requests::headers::build_conversation_headers;
use crate::requests::headers::insert_header;
use crate::requests::headers::subagent_header;
use crate::requests::responses::Compression;
use crate::requests::responses::attach_item_ids;
use crate::sse::spawn_response_stream;
use crate::telemetry::SseTelemetry;
use codex_client::HttpTransport;
use codex_client::RequestCompression;
use codex_client::RequestTelemetry;
use codex_protocol::protocol::SessionSource;
use http::HeaderMap;
use http::HeaderValue;
use http::Method;
use serde::Deserialize;
use serde_json::Value;
use std::sync::Arc;
use std::sync::OnceLock;
use tokio::sync::mpsc;

pub struct ResponsesClient<T: HttpTransport, A: AuthProvider> {
    session: EndpointSession<T, A>,
    sse_telemetry: Option<Arc<dyn SseTelemetry>>,
}

#[derive(Default)]
pub struct ResponsesOptions {
    pub conversation_id: Option<String>,
    pub session_source: Option<SessionSource>,
    pub extra_headers: HeaderMap,
    pub compression: Compression,
    pub turn_state: Option<Arc<OnceLock<String>>>,
}

impl<T: HttpTransport, A: AuthProvider> ResponsesClient<T, A> {
    pub fn new(transport: T, provider: Provider, auth: A) -> Self {
        Self {
            session: EndpointSession::new(transport, provider, auth),
            sse_telemetry: None,
        }
    }

    pub fn with_telemetry(
        self,
        request: Option<Arc<dyn RequestTelemetry>>,
        sse: Option<Arc<dyn SseTelemetry>>,
    ) -> Self {
        Self {
            session: self.session.with_request_telemetry(request),
            sse_telemetry: sse,
        }
    }

    pub async fn stream_request(
        &self,
        request: ResponsesApiRequest,
        options: ResponsesOptions,
    ) -> Result<ResponseStream, ApiError> {
        if !request.stream {
            return self.execute_request(request, options).await;
        }

        let ResponsesOptions {
            conversation_id,
            session_source,
            extra_headers,
            compression,
            turn_state,
        } = options;

        let mut body = serde_json::to_value(&request)
            .map_err(|e| ApiError::Stream(format!("failed to encode responses request: {e}")))?;
        if request.store && self.session.provider().is_azure_responses_endpoint() {
            attach_item_ids(&mut body, &request.input);
        }

        let mut headers = extra_headers;
        if let Some(ref conv_id) = conversation_id {
            insert_header(&mut headers, "x-client-request-id", conv_id);
        }
        headers.extend(build_conversation_headers(conversation_id));
        if let Some(subagent) = subagent_header(&session_source) {
            insert_header(&mut headers, "x-openai-subagent", &subagent);
        }

        self.stream(body, headers, compression, turn_state).await
    }

    async fn execute_request(
        &self,
        request: ResponsesApiRequest,
        options: ResponsesOptions,
    ) -> Result<ResponseStream, ApiError> {
        let ResponsesOptions {
            conversation_id,
            session_source,
            extra_headers,
            compression,
            ..
        } = options;

        let mut body = serde_json::to_value(&request)
            .map_err(|e| ApiError::Stream(format!("failed to encode responses request: {e}")))?;
        if request.store && self.session.provider().is_azure_responses_endpoint() {
            attach_item_ids(&mut body, &request.input);
        }

        let mut headers = extra_headers;
        if let Some(ref conv_id) = conversation_id {
            insert_header(&mut headers, "x-client-request-id", conv_id);
        }
        headers.extend(build_conversation_headers(conversation_id));
        if let Some(subagent) = subagent_header(&session_source) {
            insert_header(&mut headers, "x-openai-subagent", &subagent);
        }

        let request_compression = match compression {
            Compression::None => RequestCompression::None,
            Compression::Zstd => RequestCompression::Zstd,
        };
        let response = self
            .session
            .execute_with(Method::POST, Self::path(), headers, Some(body), |req| {
                req.compression = request_compression;
            })
            .await?;

        let parsed: ResponsesCompletedResponse =
            serde_json::from_slice(&response.body).map_err(|err| {
                ApiError::Stream(format!("failed to parse non-stream responses body: {err}"))
            })?;
        if let Some(reason) = parsed
            .incomplete_details
            .as_ref()
            .and_then(|details| details.reason.as_deref())
        {
            return Err(ApiError::Stream(format!(
                "Incomplete response returned, reason: {reason}"
            )));
        }

        let (tx_event, rx_event) = mpsc::channel(16);
        tokio::spawn(async move {
            let _ = tx_event
                .send(Ok(crate::common::ResponseEvent::Created))
                .await;
            for item in parsed.output {
                if tx_event
                    .send(Ok(crate::common::ResponseEvent::OutputItemDone(item)))
                    .await
                    .is_err()
                {
                    return;
                }
            }
            let _ = tx_event
                .send(Ok(crate::common::ResponseEvent::Completed {
                    response_id: parsed.id,
                    token_usage: parsed.usage.map(Into::into),
                }))
                .await;
        });

        Ok(ResponseStream { rx_event })
    }

    fn path() -> &'static str {
        "responses"
    }

    pub async fn stream(
        &self,
        body: Value,
        extra_headers: HeaderMap,
        compression: Compression,
        turn_state: Option<Arc<OnceLock<String>>>,
    ) -> Result<ResponseStream, ApiError> {
        let request_compression = match compression {
            Compression::None => RequestCompression::None,
            Compression::Zstd => RequestCompression::Zstd,
        };

        let stream_response = self
            .session
            .stream_with(
                Method::POST,
                Self::path(),
                extra_headers,
                Some(body),
                |req| {
                    req.headers.insert(
                        http::header::ACCEPT,
                        HeaderValue::from_static("text/event-stream"),
                    );
                    req.compression = request_compression;
                },
            )
            .await?;

        Ok(spawn_response_stream(
            stream_response,
            self.session.provider().stream_idle_timeout,
            self.sse_telemetry.clone(),
            turn_state,
        ))
    }
}

#[derive(Debug, Deserialize)]
struct ResponsesCompletedResponse {
    id: String,
    #[serde(default)]
    output: Vec<codex_protocol::models::ResponseItem>,
    #[serde(default)]
    usage: Option<ResponsesCompletedUsage>,
    #[serde(default)]
    incomplete_details: Option<ResponsesIncompleteDetails>,
}

#[derive(Debug, Deserialize)]
struct ResponsesCompletedUsage {
    input_tokens: i64,
    input_tokens_details: Option<ResponsesCompletedInputTokensDetails>,
    output_tokens: i64,
    output_tokens_details: Option<ResponsesCompletedOutputTokensDetails>,
    total_tokens: i64,
}

impl From<ResponsesCompletedUsage> for codex_protocol::protocol::TokenUsage {
    fn from(val: ResponsesCompletedUsage) -> Self {
        Self {
            input_tokens: val.input_tokens,
            cached_input_tokens: val
                .input_tokens_details
                .map(|d| d.cached_tokens)
                .unwrap_or(0),
            output_tokens: val.output_tokens,
            reasoning_output_tokens: val
                .output_tokens_details
                .map(|d| d.reasoning_tokens)
                .unwrap_or(0),
            total_tokens: val.total_tokens,
        }
    }
}

#[derive(Debug, Deserialize)]
struct ResponsesCompletedInputTokensDetails {
    cached_tokens: i64,
}

#[derive(Debug, Deserialize)]
struct ResponsesCompletedOutputTokensDetails {
    reasoning_tokens: i64,
}

#[derive(Debug, Deserialize)]
struct ResponsesIncompleteDetails {
    reason: Option<String>,
}
