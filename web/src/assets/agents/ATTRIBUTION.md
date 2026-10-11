# Agent identity marks

These marks identify the connected provider. They are not Amo branding and do not imply provider endorsement. Provider trademarks remain with their respective owners.

- `dsh.svg`: DeepSeek's whale, taken from the inline vector logo on [DeepSeek's official website](https://www.deepseek.com/) on 2026-10-11. The whale group's paths and clipping are preserved; the surrounding wordmark is omitted. Blue `#4D6BFE` is the color in the [official DeepSeek-LLM logo asset](https://github.com/deepseek-ai/DeepSeek-LLM/blob/main/images/logo.svg). The UI labels the engine **DSH**.
- `codex.svg`: OpenAI's monochrome Blossom, taken from the inline vector on the [official Codex documentation](https://developers.openai.com/codex/) on 2026-10-11. This is the OpenAI mark used to identify Codex, not a claim that it is a distinct Codex app icon. Geometry is unchanged; black/white presentation follows [OpenAI's brand guidelines](https://openai.com/brand/).

Together these SVGs are approximately 5.6 KiB before compression. They are bundled as separately cached local SVG assets (rather than duplicating inline data URIs in every sidebar row); Codex uses a current-color CSS mask to avoid Safari SVG filter rasterization. They share one source across all badges, have fixed layout dimensions and no animation, scripts, fonts, raster data or external resource references. No icon library or runtime provider request is required.
