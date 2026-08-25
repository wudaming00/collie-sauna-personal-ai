# Providers

Collie is model-agnostic. Pick a provider in the first-run onboarding, the Settings panel, per run
with `--provider`, or by setting `COLLIE_PROVIDER`. An explicit environment variable always wins.

## Connect an existing subscription

| Provider | Value | How |
|---|---|---|
| Claude Agent SDK | `claude-agent-sdk` | Embedded official SDK route for Opus; Collie supplies the system prompt, owns the tools and loop, and uses an eligible signed-in Claude Pro/Max plan. Recommended for Collie-native and overnight work. |
| ChatGPT / Codex subscription | `codex-oauth` | One-click OAuth — uses your ChatGPT plan. |
| Claude Code | `claude-cli` | Official Claude Code CLI route through `claude -p`; Collie's prompt replaces the default prompt and built-in tools are disabled. Use this when you specifically want the Claude Code transport. |

A subscription login is not itself proof of zero marginal charge. Provider policy and account
settings can change; unattended `--no-paid-overage` runs use a fail-closed preflight and never
silently switch to an API key, paid credits, or another provider.

## API-key providers

Set the matching environment variable (or paste the key in onboarding):

| Provider | Value | Key |
|---|---|---|
| Anthropic API | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Google Gemini | `gemini` | `GEMINI_API_KEY` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| Qwen / DashScope | `qwen` | `DASHSCOPE_API_KEY` |
| OpenRouter (many models) | `openrouter` | `OPENROUTER_API_KEY` |
| Moonshot / Kimi | `moonshot` | `MOONSHOT_API_KEY` |
| Zhipu GLM | `zhipu` | `ZHIPU_API_KEY` |
| Groq | `groq` | `GROQ_API_KEY` |
| Any OpenAI-compatible endpoint | `openai-compat` | endpoint + key in Settings |

## Local & offline

| Provider | Value | Notes |
|---|---|---|
| Ollama | `ollama` | Local models — nothing leaves the machine. No key. |
| Mock | `mock` | Offline, canned responses. Testing only — never real work. |

```bash
# per-run
collie run "summarize app.py" --provider ollama --model qwen2.5-coder:7b
DEEPSEEK_API_KEY=... collie -p "fix the bug"            # provider inferred from the key

# persist a choice
collie config PROVIDER claude-agent-sdk
COLLIE_PROVIDER=deepseek collie                          # env override wins for this session
```

!!! note "Native Opus overnight isolation"
    `claude-agent-sdk` invokes the official Claude Agent SDK directly; it does not shell out to
    `claude -p` and does not copy a bearer token into a raw Messages request. Collie passes its own
    replacement system prompt and sets `setting_sources=[]`. SDK built-in tools, skills, plugins,
    agents, slash commands, and fallback model are disabled; the SDK init event must attest that
    those foreign surfaces are empty before its answer is accepted. The SDK is a one-message
    reasoner inside Collie's own tool loop.

    In `--no-paid-overage` mode, API keys and provider/routing overrides are removed from the worker
    environment, and there is no API-key, paid-credit, provider, or model fallback. The route uses
    an eligible signed-in Pro/Max plan (tested on Max) and therefore remains subject to plan limits. A short
    end-to-end test proves the configured route can complete a bounded call; it is not a 12-hour
    soak, an unlimited-usage promise, or a guarantee that future provider policy will not change.
    See [Anthropic's current Claude Agent SDK plan guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

!!! note "Claude Agent SDK versus Claude Code"
    `claude-agent-sdk` embeds Anthropic's official SDK while Collie remains the agent and owns the
    tools. `claude-cli` delegates each model turn to the official Claude Code CLI subprocess. Both
    use the signed-in Claude plan, but only the SDK route is the native overnight transport.

    The former raw OAuth product route (`anthropic-oauth`, shown as "Claude direct") is removed.
    Existing saved settings are migrated to `claude-agent-sdk`; the internal identifier remains
    readable only so historical receipts and older settings do not break.

## Picking a model

Each provider has a sensible default model; override with `--model` or in Settings. The web GUI's
model picker lists what each connected provider exposes.
