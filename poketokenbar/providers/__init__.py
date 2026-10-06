from .antigravity import AntigravityProvider
from .base import UsageProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .hermes import HermesProvider
from .opencode import OpencodeProvider

# Registry. Adding a source means adding an implementation and an entry here —
# never a branch on a provider id in shared code.
# See docs/reference/provider-extension.md.
PROVIDERS: list[UsageProvider] = [
    ClaudeProvider(),
    CodexProvider(),
    AntigravityProvider(),
    OpencodeProvider(),
    HermesProvider(),
]

__all__ = [
    "PROVIDERS",
    "AntigravityProvider",
    "ClaudeProvider",
    "CodexProvider",
    "HermesProvider",
    "OpencodeProvider",
    "UsageProvider",
]
