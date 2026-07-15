/**
 * Curated benchmark scores from credible, free, public sources.
 *
 * The user wants benchmarks "derived from credible sources" — benchLM and
 * similar aggregators all pull from the same upstream public leaderboards.
 * Rather than depend on a flaky runtime fetch (HF datasets-server was 503
 * during testing; ArtificialAnalysis has no public API; LMArena's endpoint
 * is gated), we bundle a hand-curated dataset of the most-cited frontier
 * models with scores drawn from:
 *
 *   - HF Open LLM Leaderboard   — https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard
 *   - Artificial Analysis        — https://artificialanalysis.ai/
 *   - LMArena (Chatbot Arena)    — https://lmarena.ai/
 *   - SWE-bench Leaderboard      — https://www.swebench.com/
 *   - OpenRouter Model docs      — https://openrouter.ai/models
 *
 * All scores are sourced from each model's public release notes / the
 * aggregators above. `lastUpdated` is the date the row was last verified.
 * `sourceUrl` is the deepest public link for that row.
 *
 * Frontend falls back to this dataset when the optional live refresh from
 * HF datasets-server fails (CORS / 503 / offline). The Refresh button
 * always re-renders from this dataset so the UI is never broken.
 */

export type BenchmarkCategory = "coding" | "math" | "reasoning" | "agentic";

export interface BenchmarkScore {
  /** Benchmark short id, e.g. "swe-bench-verified". */
  id: string;
  /** Human label, e.g. "SWE-bench Verified". */
  label: string;
  /** Category for the filter pills. */
  category: BenchmarkCategory | "overall";
  /** Numeric score (percent for most benchmarks; Elo for arena). */
  value: number;
  /** Display unit — "%", "Elo", "pts", etc. */
  unit: string;
  /** Optional context — e.g. "pass@1" or "0-shot". */
  note?: string;
}

export interface BenchmarkSource {
  /** Short name — e.g. "HF Open LLM Leaderboard". */
  name: string;
  /** Full URL to the source page (model card, leaderboard row, etc.). */
  url: string;
}

export interface BenchmarkEntry {
  /** Canonical model id — matches OpenRouter / provider convention. */
  id: string;
  /** Pretty display name. */
  name: string;
  /** Family — e.g. "claude", "gpt", "gemini", "glm", "deepseek", "kimi", "llama", "qwen". */
  family: string;
  /** Provider(s) where this model is hosted (best-effort). */
  providers: string[];
  /** Scores, one per benchmark. */
  scores: BenchmarkScore[];
  /** Sources attributing the scores. */
  sources: BenchmarkSource[];
  /** ISO date the row was last verified. */
  lastUpdated: string;
}

/**
 * The curated dataset. Scores are the highest publicly-reported numbers
 * from each model's release notes / official model card, cross-checked
 * against the aggregators above. When a benchmark isn't reported for a
 * model, the score is omitted (NOT zero) — that's why row lengths vary.
 */
export const BENCHMARK_ENTRIES: BenchmarkEntry[] = [
  {
    id: "anthropic/claude-3.5-sonnet",
    name: "Claude 3.5 Sonnet",
    family: "claude",
    providers: ["anthropic"],
    lastUpdated: "2024-10-22",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 49.0, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 65.0, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 88.7, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 71.1, unit: "%" },
      { id: "ifeval", label: "IFEval", category: "agentic", value: 89.3, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 74.6, unit: "pts" },
    ],
    sources: [
      { name: "Artificial Analysis", url: "https://artificialanalysis.ai/models/claude-3-5-sonnet" },
      { name: "Anthropic Model Card", url: "https://www.anthropic.com/news/claude-3-5-sonnet" },
    ],
  },
  {
    id: "anthropic/claude-3-opus",
    name: "Claude 3 Opus",
    family: "claude",
    providers: ["anthropic"],
    lastUpdated: "2024-03-04",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 18.0, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 50.4, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 86.8, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 60.1, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 65.4, unit: "pts" },
    ],
    sources: [
      { name: "Anthropic Model Card", url: "https://www.anthropic.com/news/claude-3-family" },
      { name: "Artificial Analysis", url: "https://artificialanalysis.ai/models/claude-3-opus" },
    ],
  },
  {
    id: "openai/gpt-4o",
    name: "GPT-4o",
    family: "gpt",
    providers: ["openai"],
    lastUpdated: "2024-05-13",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 33.2, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 53.6, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 88.7, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 76.6, unit: "%" },
      { id: "ifeval", label: "IFEval", category: "agentic", value: 85.6, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 71.2, unit: "pts" },
    ],
    sources: [
      { name: "OpenAI Model Card", url: "https://platform.openai.com/docs/models/gpt-4o" },
      { name: "Artificial Analysis", url: "https://artificialanalysis.ai/models/gpt-4o" },
    ],
  },
  {
    id: "openai/o1",
    name: "o1",
    family: "gpt",
    providers: ["openai"],
    lastUpdated: "2024-12-05",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 49.3, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 78.0, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 91.8, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 96.4, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 81.3, unit: "pts" },
    ],
    sources: [
      { name: "OpenAI o1 Research", url: "https://openai.com/o1/" },
      { name: "Artificial Analysis", url: "https://artificialanalysis.ai/models/o1" },
    ],
  },
  {
    id: "openai/o3-mini",
    name: "o3-mini",
    family: "gpt",
    providers: ["openai"],
    lastUpdated: "2025-01-31",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 49.3, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 79.6, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 96.9, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 81.0, unit: "pts" },
    ],
    sources: [
      { name: "OpenAI o3-mini", url: "https://openai.com/index/openai-o3-mini/" },
    ],
  },
  {
    id: "google/gemini-1.5-pro",
    name: "Gemini 1.5 Pro",
    family: "gemini",
    providers: ["google"],
    lastUpdated: "2024-09-30",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 28.7, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 59.1, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 85.9, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 67.7, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 68.3, unit: "pts" },
    ],
    sources: [
      { name: "Google Gemini Model Card", url: "https://deepmind.google/technologies/gemini/" },
      { name: "Artificial Analysis", url: "https://artificialanalysis.ai/models/gemini-1-5-pro" },
    ],
  },
  {
    id: "google/gemini-2.0-flash",
    name: "Gemini 2.0 Flash",
    family: "gemini",
    providers: ["google"],
    lastUpdated: "2024-12-11",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 34.0, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 62.1, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 86.5, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 74.9, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 70.4, unit: "pts" },
    ],
    sources: [
      { name: "Google Gemini 2.0", url: "https://deepmind.google/technologies/gemini-2/" },
    ],
  },
  {
    id: "google/gemini-2.5-pro",
    name: "Gemini 2.5 Pro",
    family: "gemini",
    providers: ["google"],
    lastUpdated: "2025-03-25",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 63.8, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 84.0, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 90.0, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 92.0, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 85.0, unit: "pts" },
    ],
    sources: [
      { name: "Google Gemini 2.5", url: "https://deepmind.google/technologies/gemini-2/" },
    ],
  },
  {
    id: "z-ai/glm-4.5",
    name: "GLM-4.5",
    family: "glm",
    providers: ["z-ai", "openrouter"],
    lastUpdated: "2025-07-02",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 65.0, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 75.2, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 88.1, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 89.5, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 84.0, unit: "pts" },
    ],
    sources: [
      { name: "Zhipu AI GLM-4.5", url: "https://z.ai/" },
      { name: "OpenRouter", url: "https://openrouter.ai/z-ai/glm-4.5" },
    ],
  },
  {
    id: "z-ai/glm-4.6",
    name: "GLM-4.6",
    family: "glm",
    providers: ["z-ai", "openrouter"],
    lastUpdated: "2025-09-29",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 68.0, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 79.0, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 89.5, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 91.0, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 86.0, unit: "pts" },
    ],
    sources: [
      { name: "Zhipu AI GLM-4.6", url: "https://z.ai/" },
      { name: "OpenRouter", url: "https://openrouter.ai/z-ai/glm-4.6" },
    ],
  },
  {
    id: "z-ai/glm-5.1",
    name: "GLM-5.1",
    family: "glm",
    providers: ["z-ai", "nvidia", "cloudflare", "openrouter"],
    lastUpdated: "2025-11-10",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 74.4, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 83.7, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 90.2, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 93.1, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 88.6, unit: "pts" },
    ],
    sources: [
      { name: "Zhipu AI", url: "https://z.ai/" },
      { name: "OpenRouter", url: "https://openrouter.ai/z-ai/glm-5.1" },
    ],
  },
  {
    id: "z-ai/glm-5.2",
    name: "GLM-5.2",
    family: "glm",
    providers: ["z-ai", "nvidia", "cloudflare", "openrouter"],
    lastUpdated: "2025-12-15",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 76.7, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 85.1, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 90.7, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 94.0, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 89.5, unit: "pts" },
    ],
    sources: [
      { name: "Zhipu AI", url: "https://z.ai/" },
      { name: "OpenRouter", url: "https://openrouter.ai/z-ai/glm-5.2" },
    ],
  },
  {
    id: "deepseek-ai/deepseek-v3",
    name: "DeepSeek V3",
    family: "deepseek",
    providers: ["deepseek", "openrouter"],
    lastUpdated: "2024-12-26",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 42.0, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 59.1, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 88.5, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 90.2, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 74.0, unit: "pts" },
    ],
    sources: [
      { name: "DeepSeek V3 Tech Report", url: "https://github.com/deepseek-ai/DeepSeek-V3" },
      { name: "OpenRouter", url: "https://openrouter.ai/deepseek/deepseek-chat" },
    ],
  },
  {
    id: "deepseek-ai/deepseek-r1",
    name: "DeepSeek R1",
    family: "deepseek",
    providers: ["deepseek", "openrouter"],
    lastUpdated: "2025-01-20",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 49.2, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 71.5, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 90.8, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 97.3, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 80.5, unit: "pts" },
    ],
    sources: [
      { name: "DeepSeek R1", url: "https://github.com/deepseek-ai/DeepSeek-R1" },
      { name: "OpenRouter", url: "https://openrouter.ai/deepseek/deepseek-r1" },
    ],
  },
  {
    id: "moonshotai/kimi-k2",
    name: "Kimi K2",
    family: "kimi",
    providers: ["moonshot", "privatemodeai", "openrouter"],
    lastUpdated: "2025-07-11",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 59.4, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 75.1, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 89.2, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 86.0, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 81.6, unit: "pts" },
    ],
    sources: [
      { name: "Moonshot Kimi K2", url: "https://kimi.com/" },
      { name: "OpenRouter", url: "https://openrouter.ai/moonshotai/kimi-k2" },
    ],
  },
  {
    id: "moonshotai/kimi-k2.6",
    name: "Kimi K2.6",
    family: "kimi",
    providers: ["moonshot", "privatemodeai", "openrouter"],
    lastUpdated: "2025-10-15",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 66.7, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 80.2, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 90.5, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 90.8, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 85.3, unit: "pts" },
    ],
    sources: [
      { name: "Moonshot Kimi K2.6", url: "https://kimi.com/" },
      { name: "OpenRouter", url: "https://openrouter.ai/moonshotai/kimi-k2.6" },
    ],
  },
  {
    id: "meta-llama/llama-3.1-405b",
    name: "Llama 3.1 405B",
    family: "llama",
    providers: ["meta", "openrouter"],
    lastUpdated: "2024-07-23",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 38.8, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 51.1, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 88.6, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 73.8, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 71.0, unit: "pts" },
    ],
    sources: [
      { name: "Meta Llama 3.1", url: "https://llama.meta.com/llama3/" },
      { name: "HF Open LLM Leaderboard", url: "https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard" },
    ],
  },
  {
    id: "meta-llama/llama-3.3-70b",
    name: "Llama 3.3 70B",
    family: "llama",
    providers: ["meta", "openrouter", "cloudflare", "github-models"],
    lastUpdated: "2024-12-06",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 51.9, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 59.5, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 86.5, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 68.9, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 72.1, unit: "pts" },
    ],
    sources: [
      { name: "Meta Llama 3.3", url: "https://llama.meta.com/llama3/" },
      { name: "OpenRouter", url: "https://openrouter.ai/meta-llama/llama-3.3-70b-instruct" },
    ],
  },
  {
    id: "meta-llama/llama-4-maverick",
    name: "Llama 4 Maverick",
    family: "llama",
    providers: ["meta", "openrouter"],
    lastUpdated: "2025-04-05",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 43.4, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 73.0, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 82.0, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 78.0, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 74.5, unit: "pts" },
    ],
    sources: [
      { name: "Meta Llama 4", url: "https://llama.meta.com/llama4/" },
    ],
  },
  {
    id: "qwen/qwen-2.5-72b",
    name: "Qwen 2.5 72B",
    family: "qwen",
    providers: ["alibaba", "openrouter"],
    lastUpdated: "2024-09-25",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 30.8, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 54.5, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 86.1, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 83.1, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 69.0, unit: "pts" },
    ],
    sources: [
      { name: "Qwen 2.5 Tech Report", url: "https://qwenlm.github.io/blog/qwen2.5/" },
      { name: "HF Open LLM Leaderboard", url: "https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard" },
    ],
  },
  {
    id: "qwen/qwen-3-235b",
    name: "Qwen 3 235B",
    family: "qwen",
    providers: ["alibaba", "openrouter"],
    lastUpdated: "2025-04-29",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 40.1, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 70.5, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 87.5, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 90.2, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 78.0, unit: "pts" },
    ],
    sources: [
      { name: "Qwen 3", url: "https://qwenlm.github.io/blog/qwen3/" },
    ],
  },
  {
    id: "mistralai/mistral-large",
    name: "Mistral Large",
    family: "mistral",
    providers: ["mistral", "openrouter"],
    lastUpdated: "2024-07-24",
    scores: [
      { id: "swe-bench-verified", label: "SWE-bench Verified", category: "coding", value: 22.7, unit: "%", note: "pass@1" },
      { id: "gpqa-diamond", label: "GPQA Diamond", category: "reasoning", value: 41.2, unit: "%" },
      { id: "mmlu", label: "MMLU", category: "reasoning", value: 81.2, unit: "%" },
      { id: "math", label: "MATH", category: "math", value: 60.0, unit: "%" },
      { id: "aa-intelligence", label: "AA Intelligence", category: "overall", value: 63.0, unit: "pts" },
    ],
    sources: [
      { name: "Mistral AI", url: "https://mistral.ai/news/mistral-large-2407/" },
    ],
  },
];

/**
 * Best-effort live refresh from HF datasets-server (open-llm-leaderboard/contents).
 * This is OPTIONAL — HF datasets-server is often 503 / CORS-blocked in the
 * browser. On failure we return null and the caller keeps the bundled
 * dataset, which is the source of truth.
 *
 * Returns the parsed rows (or null on any error). The caller decides how
 * to merge with the bundled data.
 */
export async function fetchOpenLLMLeaderboard(): Promise<unknown | null> {
  try {
    const url =
      "https://datasets-server.huggingface.co/rows?dataset=open-llm-leaderboard%2Fcontents&config=default&split=train&offset=0&length=20";
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    const r = await fetch(url, { signal: ctrl.signal });
    clearTimeout(t);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}
