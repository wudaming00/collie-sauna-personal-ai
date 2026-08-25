# Sauna cost model and the Collie local-runtime thesis

Research snapshot: 2026-08-24. This is a product and architecture analysis, not a claim about
Sauna's unpublished financials.

## What the public evidence supports

Sauna does not publish COGS, model-provider bills, or a conversion from credits to tokens. It is
therefore not defensible to say that most of Sauna's cost *is* API tokens.

What is public:

- Sauna's current public pricing page lists Lite at $29 / 1,200 credits, Basic at $99 / 4,000,
  Pro at $299 / 12,000, and Team at $999 / 40,000. All plans include persistent memory,
  iMessage/Slack connections, 3,000+ integrations, and cloud storage; higher plans add broader
  model access, shared context, and browser automation.
- Sauna's terms say that different operations consume different credit amounts according to the
  work involved, but do not publish a per-operation rate card. Every top-up pack prices a credit
  at $0.05.
- The same terms describe third-party model inference, a cloud sandbox, browser operation,
  persistent memory, connected services, and scheduled/autonomous work.

The reasonable inference is that model inference is likely a major variable cost, especially for
long, parallel agent runs, but it is not the entire service cost. Sandboxes, browser sessions,
storage, sync, integrations, security, observability, and support remain real costs.

There is also a public-data inconsistency: the marketing pricing page currently says
4,000 / 12,000 / 40,000 monthly credits for Basic / Pro / Team, while the June 2026 terms still
say 6,000 / 18,000 / 60,000. The in-app billing screen should be treated as authoritative.

Sources:

- https://www.sauna.ai/pricing
- https://www.sauna.ai/terms

## Is customer-subscription execution more reasonable?

Yes, for work that happens interactively on the customer's own device through an official client.
It should not be the only execution lane.

Claude Code officially supports signing into a Claude Pro or Max subscription. Codex officially
supports signing in with eligible ChatGPT plans. That makes a local runtime commercially useful:
many users already have capable, paid inference available on their device.

But this is not permission to proxy or resell consumer subscriptions. OpenAI's consumer terms do
not allow sharing account credentials or programmatically extracting output. Anthropic's consumer
terms do not allow sharing credentials, reselling the service, or automated/non-human access
unless explicitly permitted. Collie should invoke the user's installed, official client locally;
it must never upload, store, or replay a user's consumer session in Sauna's cloud.

Sources:

- https://support.anthropic.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
- https://docs.anthropic.com/en/docs/claude-code/getting-started
- https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- https://www.anthropic.com/legal/consumer-terms
- https://openai.com/policies/terms-of-use/

## Recommended three-lane architecture

1. **Local subscription lane — default for interactive work.** Collie routes work to an official
   Claude Code or Codex client already authenticated by that user. Credentials remain provider-
   owned and device-local. The customer receives the value of a subscription they already pay for.
2. **Customer API lane — optional for power users and companies.** A user or organization can
   supply an API key under the provider's commercial terms, with explicit budgets and usage
   receipts. Secrets remain in the OS credential store or an approved enterprise vault.
3. **Sauna managed lane — paid and metered.** Sauna supplies inference for mobile, offline,
   scheduled, always-on, parallel, or fallback work. This is the lane where managed credits make
   sense because Sauna owns both reliability and variable compute cost.

Routing should be deterministic and visible. Local files and logged-in browser work prefer local;
work that must continue with the device off prefers cloud; external side effects always retain the
same approval policy. A receipt records the lane, model, payer, budget, and result.

## Product and pricing implication

Collie can be a free, local runtime. Sauna should charge for the durable *person layer*: encrypted
sync, memory and provenance, people/project graph, integrations, multi-device continuity,
collaboration, proactive scheduling, cloud execution, and reliability.

Do not promise "bring your subscription, unlimited AI." Subscription limits still apply, provider
availability changes, and the local device may be asleep. A better promise is:

> Use the intelligence you already pay for when your device can do the work. Pay Sauna for the
> continuity, context, integrations, and cloud execution that make it a personal AI system.

This lowers Sauna's inference exposure on eligible local work, reduces perceived double-payment,
and gives Collie a strong free distribution loop. It also preserves a clean paid upgrade when work
must outlive the device.

## Interview answer

> I would not position Collie as a way to arbitrage consumer subscriptions. I would position it as
> Sauna's local runtime: it can route authorized work to official model clients the customer already
> has, while Sauna remains the paid person-level cloud for memory, sync, integrations, continuity,
> and always-on execution. Local work reduces managed inference cost; cloud work stays metered.
> Same state model, three execution lanes, and a receipt showing who ran and paid for every task.
