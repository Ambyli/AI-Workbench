# Welcome to Zeo Chat

Zeo Chat is our internal AI workspace. It runs on Open WebUI in front of a mix of local (Zeo-hosted) and hosted models. This page gets you productive in about five minutes.

## 1. Pick the right model

The model picker (top-left of every chat) has two options:

| Model | Good for | Notes |
|---|---|---|
| `qwen3.8` | Everyday chat, drafting, summarising, code, reasoning, images | Local, unlimited — use this by default |
| `claude-sonnet-5` | Fallback when `qwen3.8` can't get the answer right | Hosted (Anthropic), costs money per message — use sparingly |

**Rule of thumb:** always start with `qwen3.8`. Only switch to `claude-sonnet-5` after `qwen3.8` has clearly failed on the task.

## 2. Start a chat

- Type a message and hit enter.
- The **+** icon (bottom-left of the input box) attaches files: PDFs, spreadsheets, images, code files. The model reads them directly.
- Older chats appear in the left sidebar. Rename, pin, or archive them from the three-dot menu.
- **New Chat** (top-left) clears the context — previous chats don't leak in.

Tip: models don't remember anything between chats. If you want them to know something about you or your work every time, set it in **Settings → Personalization → Memory**.

## 3. Tools you can toggle

Under the chat input, click the **Tools** icon to enable extras for the current message:

- **Web Search** — the model runs live searches (via our internal SearxNG) before answering. Use for anything time-sensitive or fact-checkable.
- **Phoenix** — lets the model query our Phoenix database directly. Ask things like *"how many active migrations do we have this week?"* and it will run the SQL for you. Read-only.
- **Preview App** — hand the model a description and it will build a small live web app (Streamlit, React, static HTML, etc.) and embed it in the chat. Good for one-off dashboards and prototypes.

Only turn on the tools you need — each one costs tokens and slows the reply.

## 4. What NOT to put in

- **Customer PII** beyond what's already in Phoenix. If it wouldn't fit in a Slack `#general` post, don't paste it here.
- **Credentials, tokens, API keys.** The chat history is stored on our server and searchable.
- **Anything from a signed NDA with a third party** unless legal has cleared it.

Everything you type is logged. Assume a coworker could read your chat history.

## 5. Getting help

For anything Zeo Chat related — bugs, access issues, new-model requests, feature ideas — open a ticket at <https://helpdesk.zeoenergy.com>.

---

## Power-user extras

- **Prompt library** — **Workspace → Prompts** stores reusable prompt templates. Start with `/` in any chat to insert one.
- **Model files** — **Workspace → Models** lets you save a model + system-prompt combo as its own entry in the picker (e.g. "Zeo Marketing Copy" = `qwen3.6` + tone guidelines).
- **Keyboard shortcuts** — `Ctrl+Shift+O` new chat, `Ctrl+Shift+S` toggle sidebar, `Ctrl+/` shortcut help.
- **Desktop app** — <https://github.com/open-webui/desktop> gives you a native window; point it at `https://chat.zeoenergy.com`.
