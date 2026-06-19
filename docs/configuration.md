# Configuration

## Environment Variables
- `OPENAI_BASE_URL`: Base URL for OpenAI-compatible endpoint (self-hosted or hosted).
- `OPENAI_API_KEY`: API key.
- `MODEL`: Model name.
- ERPNext connection vars (host, key, etc.).
- Beancount connection vars (path, repo, etc.).

## LLM Endpoint
Point `OPENAI_BASE_URL` at a self-hosted vLLM/Ollama instance or a hosted provider.

## Guardrails
Confidence-threshold settings control auto-approval vs Human Review Queue.

## Running
See `.env.example`, `docker-compose.yml`, and `install.sh`.