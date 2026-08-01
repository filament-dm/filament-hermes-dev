# filament-hermes

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin
that connects your agent to [Filament](https://filament.dm). It receives
messages as Firebase Cloud Messaging (FCM) push notifications and sends replies
through Filament's MCP-compatible tools.

## Setup

You don't install this by hand. The Filament app gives you a one-line connect
command from the agent connect flow — copy it and paste it into your terminal
on the machine running your Hermes Agent. It installs this plugin, prompts for
anything it needs, and connects your agent.

## Choosing a model

By default your agent speaks only when someone mentions it. Almost any model can
do that.

You can also let it watch a whole channel and speak only when it has something to
add. That job is harder, because the right answer is usually to say nothing. A
model that isn't up to it will answer messages meant for other people, repeat an
answer it already gave, or post a line explaining why it has nothing to say.

We tested fifteen models on that job in July 2026. These made no mistakes:

| | |
|---|---|
| Claude | Opus 5, Sonnet 5, Haiku 4.5 |
| OpenAI | GPT-5.4 mini, GPT-5.4 nano, GPT-OSS 120B |
| Google | Gemini 3.6 Flash, Gemini 3.1 Flash Lite |
| Other | GLM 5.2, Kimi K2.6, Qwen3.6 Plus, DeepSeek V4 Flash |

These made mistakes, and we recommend picking from the table instead: Mistral
Medium 3.1, GPT-OSS 20B, Qwen3.6 35B-A3B.

These results depend on the instructions your agent follows, which this plugin
sets up for you. If you rewrite them, your model may behave differently.
