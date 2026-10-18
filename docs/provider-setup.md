# Research provider setup (Groq, Tavily, Brave)

Research works without any keys, but in a reduced mode:
- **Without a Groq key**, fact extraction runs only on the local model, which is private but takes minutes per page on
  a CPU-only laptop.
- **Without a search key**, company research uses only each company's own website, and individuals can only be found
  on that site.

Add the keys below for fast, independent, corroborated research. All three have free allowances.

> **Keep keys secret.** They go only into the `.env` file on your machine, which git ignores. Never paste them into
> chat, tickets, screenshots, or code. If a key leaks, delete it in the provider's dashboard and create a new one.

## 1. Groq (LLM): free, no card

1. Go to **https://console.groq.com** and sign in (Google, GitHub, or email).
2. Open **API Keys** in the left menu and click **Create API Key**. Name it `outreach-agent`.
3. **Copy the key now.** It starts with `gsk_` and is shown only once.
4. Recommended: open **Settings → Data Controls** and turn on **Zero Data Retention**. Groq's terms already bar
   training on API data; this also stops prompts being kept for abuse monitoring.
5. Free-tier limits are per model and per day (about 1,000 requests and 200K tokens per model at the time of writing;
   check **Settings → Limits** for your exact numbers). The app spreads work across several models and switches to the
   local model when a daily quota runs out.

## 2. Tavily (web search): free, no card

1. Go to **https://app.tavily.com** and sign up.
2. Your API key is shown on the dashboard home page. It starts with `tvly-`. Copy it.
3. The free **Researcher** plan includes 1,000 credits per month. A basic search costs 1 credit, and results are cached
   for 24 hours, so repeated searches are free.

## 3. Brave Search API (web search, independent index): card required

1. Go to **https://api-dashboard.search.brave.com** and create an account (verify your email).
2. Subscribe to the **Search** plan. Brave asks for a card to deter abuse, and the plan includes about **$5 of free
   credit every month** (roughly 1,000 searches). Check the dashboard's usage page; if it offers a spending limit,
   set one.
3. Open **API Keys → Add API key**, select the Search subscription, and copy the key.
4. The app uses Brave after Tavily (`SEARCH_PROVIDER_ORDER=tavily,brave`) and switches automatically when Tavily's
   monthly credits run out.

## 4. Put the keys in `.env`

The `.env` file is at the project root: `C:\Users\user\projects\outreach-agent\.env`.

1. Open it in an editor:
   ```powershell
   code C:\Users\user\projects\outreach-agent\.env      # VS Code
   # or: notepad C:\Users\user\projects\outreach-agent\.env
   ```
2. Add (or fill in) these lines: no quotes, no spaces around `=`, one key per line.
   ```dotenv
   GROQ_API_KEY=gsk_your_key_here
   TAVILY_API_KEY=tvly-your_key_here
   BRAVE_API_KEY=your_brave_key_here
   ```
   Leave a line out if you don't have that key yet. Everything else has sensible defaults (see `.env.example`).
3. Save the file, then restart the services that read it:
   ```powershell
   cd C:\Users\user\projects\outreach-agent
   docker compose up -d --build backend worker
   ```
4. Check it worked:
   - In the dashboard, open **Research → Providers**. Each configured provider shows **Configured**.
   - Or check the worker log (key *names* only; values are never logged):
     ```powershell
     docker compose logs worker | Select-String providers_configured
     ```
5. Research that failed earlier because a provider was missing can be re-run from **Research → Retry failed**.

## Local model (Ollama): already installed

Ollama runs natively on Windows (so it can use the host's hardware) at `http://127.0.0.1:11434`. The worker container
reaches it at `host.docker.internal`. The default fallback model is `qwen3:4b`. It was the most accurate of the four
small models benchmarked on this laptop's research workload (5/5 facts found, verbatim quotes, rumors and injected
instructions correctly handled), but it runs at about 5 tokens/second here, so it's a spill-over, not the primary
engine.

To change the model order, edit `LLM_EXTRACTION_CHAIN` in `.env`, for example to use only Groq:
```dotenv
LLM_EXTRACTION_CHAIN=groq:openai/gpt-oss-20b,groq:qwen/qwen3.8-27b
```
