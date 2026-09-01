from .antigravity import AntigravityProvider
from .base import UsageProvider
from .claude import ClaudeProvider
from .codex import CodexProvider

# Registry. Adding a source means adding an implementation and an entry here —
# never a branch on a provider id in shared code.
# See docs/reference/provider-extension.md.
PROVIDERS: list[UsageProvider] = [ClaudeProvider(), CodexProvider(), AntigravityProvider()]

__all__ = ["PROVIDERS", "AntigravityProvider", "ClaudeProvider", "CodexProvider", "UsageProvider"]
