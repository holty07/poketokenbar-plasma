from .antigravity import AntigravityProvider
from .aside import AsideProvider
from .base import UsageProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .copilot import CopilotProvider
from .crush import CrushProvider
from .cursor import CursorProvider
from .gemini import GeminiProvider
from .grok import GrokProvider
from .hermes import HermesProvider
from .jcode import JcodeProvider
from .kimi_code import KimiCodeProvider
from .kiro import KiroProvider
from .omp import OmpProvider
from .opencode import OpencodeProvider
from .pi import PiProvider

# Registry. Adding a source means adding an implementation and an entry here —
# never a branch on a provider id in shared code.
# See docs/reference/provider-extension.md.
PROVIDERS: list[UsageProvider] = [
    ClaudeProvider(),
    CodexProvider(),
    AntigravityProvider(),
    OpencodeProvider(),
    HermesProvider(),
    KimiCodeProvider(),
    PiProvider(),
    OmpProvider(),
    AsideProvider(),
    KiroProvider(),
    CursorProvider(),
    JcodeProvider(),
    CrushProvider(),
    GeminiProvider(),
    GrokProvider(),
    CopilotProvider(),
]

__all__ = [
    "PROVIDERS",
    "AntigravityProvider",
    "AsideProvider",
    "ClaudeProvider",
    "CodexProvider",
    "CopilotProvider",
    "CrushProvider",
    "CursorProvider",
    "GeminiProvider",
    "GrokProvider",
    "HermesProvider",
    "JcodeProvider",
    "KimiCodeProvider",
    "KiroProvider",
    "OmpProvider",
    "OpencodeProvider",
    "PiProvider",
    "UsageProvider",
]
